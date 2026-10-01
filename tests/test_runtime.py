"""The collector runtime core (F2): write path, watermarks, lock, heartbeat, liveness.

reconcile.py and attribution.py are replaced with recording fakes, so these
tests cover the runtime's own behaviour whether or not those modules exist.
"""

import importlib
import sqlite3
import sys
import threading
import time
import types

import pytest

from usage_watch import model
from usage_watch.errors import Problem
from usage_watch.runtime import core
from usage_watch.runtime.core import KEEP_PAST_RUNS, Runtime, WritePath
from usage_watch.runtime.liveness import liveness
from usage_watch.store import connect, migrate

T0 = 1_790_000_000.0  # epoch seconds


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class Calls:
    def __init__(self):
        self.log = []
        self.threads = []
        self.fail_upsert = None


@pytest.fixture
def calls(monkeypatch):
    """Replace the four collaborator functions, on the real modules if they
    import, else on stand-in modules."""
    c = Calls()
    mods = {}
    for name in ("reconcile", "attribution"):
        full = f"usage_watch.runtime.{name}"
        try:
            mod = importlib.import_module(full)
        except Exception:
            mod = types.ModuleType(full)
            monkeypatch.setitem(sys.modules, full, mod)
        mods[name] = mod
    next_id = iter(range(100, 10_000))

    def upsert_observation(conn, obs, merge=None):
        c.threads.append(threading.get_ident())
        if c.fail_upsert:
            raise c.fail_upsert
        c.log.append(("upsert", obs.source_request_key, merge))
        return next(next_id)

    def reconcile_observation(conn, observation_id, *, primary):
        c.log.append(("reconcile", observation_id, primary))
        return None

    def add_evidence(conn, ev):
        c.log.append(("add_evidence", ev.subject_kind, ev.subject_id, ev.dimension))
        return 1

    def resolve(conn, subject_kind, subject_id, dimension, subject_time):
        c.log.append(("resolve", subject_kind, subject_id, dimension, subject_time))
        return model.EffectiveAttribution(subject_kind, subject_id, dimension, "unattributed",
                                          note="fake")

    for mod, fn in ((mods["reconcile"], upsert_observation), (mods["reconcile"], reconcile_observation),
                    (mods["attribution"], add_evidence), (mods["attribution"], resolve)):
        monkeypatch.setattr(mod, fn.__name__, fn, raising=False)
    return c


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "usage.db")
    migrate(conn)
    yield conn
    conn.close()


def merge_rule(old, new):
    return new


class Pull:
    def __init__(self, name="claude.transcript", batches=None, interval_s=60, primary=True):
        self.name = name
        self.primary = primary
        self.interval_s = interval_s
        self.merge = merge_rule
        self.batches = list(batches or [])
        self.seen = []

    def collect(self, watermark):
        self.seen.append(watermark)
        if not self.batches:
            return [], watermark
        return self.batches.pop(0)


class Push:
    def __init__(self, name="otlp.receiver", primary=False):
        self.name = name
        self.primary = primary
        self.merge = None
        self.sink = None
        self.stopped = False

    def start(self, sink):
        self.sink = sink

    def stop(self):
        self.stopped = True


def obs(key="m1:r1"):
    return model.UsageObservation(source="claude.transcript", stream_key="claude:s1",
                                  source_request_key=key, confidence="authoritative",
                                  observed_at=1000, output_tokens=5)


def evidence():
    return model.AttributionEvidence(
        subject_kind="session", subject_id="claude:s1", dimension="project", value="p",
        method="cwd", source="claude.transcript", confidence="observed", validity="historical",
        first_observed_at=1234, last_confirmed_at=1300)


def state(t=2000, pane="%1"):
    return model.AgentStateSample(pane=pane, observed_at=t, state="busy", source="screen")


def one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def status(conn, name):
    return one(conn, "SELECT last_run_at, last_success_at, last_new_at, last_error"
                     " FROM collector_status WHERE collector = ?", (name,))


# --- the write path ---------------------------------------------------------------

def test_dispatch_by_item_type(db, calls):
    src = Pull()
    items = [
        obs(),
        evidence(),
        model.Session("claude:s1", "claude", "s1", cwd="/w", live=True),
        model.CapacitySample("claude.statusline", "st1", "session", "authoritative", 1000, used_pct=40.0),
        model.LimitEvent("claude.transcript", "claude:s1", "k1", "hit", "authoritative", 1000),
        state(),
        model.ContextEvent("task_start", "workmux", 1000, task="t"),
        model.CostEvent("session", "claude:s1", "omp.session", "harness_estimate", 99, "observed", 1000),
    ]
    assert WritePath(db, Clock()).apply(src, items)
    assert calls.log == [
        ("upsert", "m1:r1", merge_rule),
        ("reconcile", 100, True),
        ("add_evidence", "session", "claude:s1", "project"),
        ("resolve", "session", "claude:s1", "project", 1234),
    ]
    for table in ("sessions", "capacity_samples", "limit_events", "state_samples",
                  "context_events", "cost_events"):
        assert one(db, f"SELECT count(*) FROM {table}")[0] == 1, table
    now = int(T0 * 1000)
    assert status(db, "claude.transcript") == (now, now, now, None)


def test_secondary_source_reconciles_as_secondary(db, calls):
    WritePath(db, Clock()).apply(Pull(name="otlp", primary=False), [obs()])
    assert calls.log[1] == ("reconcile", 100, False)


def test_repeat_updates_by_identity_and_keeps_unstated_fields(db, calls):
    wp = WritePath(db, Clock())
    wp.apply(Pull(), [model.Session("claude:s1", "claude", "s1", cwd="/w", live=True),
                      model.LimitEvent("c", "s", "k1", "hit", "authoritative", 1000)])
    wp.apply(Pull(), [model.Session("claude:s1", "claude", "s1", last_seen_at=5, live=False),
                      model.LimitEvent("c", "s", "k1", "hit", "authoritative", 1000, resets_at=9)])
    assert one(db, "SELECT cwd, last_seen_at, live FROM sessions") == ("/w", 5, 0)
    assert one(db, "SELECT count(*), max(resets_at) FROM limit_events") == (1, 9)


def test_failed_batch_rolls_back_and_records_no_values(db, calls):
    wp = WritePath(db, Clock())
    assert wp.apply(Pull(), [state(1)])
    ok_at = status(db, "claude.transcript")[1]
    bad = model.CapacitySample("claude.statusline", "SECRET-stream", "session", "authoritative",
                               1000, used_pct=150.25)
    assert wp.apply(Pull(), [model.Session("claude:s9", "claude", "s9"), bad]) is False
    assert one(db, "SELECT count(*) FROM sessions")[0] == 0  # the whole batch rolled back
    err = status(db, "claude.transcript")[3]
    assert err == "IntegrityError at items[1]:CapacitySample.used_pct"
    assert status(db, "claude.transcript")[1] == ok_at  # last success is kept

    calls.fail_upsert = ValueError("output_tokens 987654 in SECRET payload")
    assert wp.apply(Pull(), [obs()]) is False
    err = status(db, "claude.transcript")[3]
    assert err == "ValueError at items[0]:UsageObservation"
    assert "987654" not in err and "SECRET" not in err


def test_unknown_item_is_a_type_error_not_a_crash(db, calls):
    assert WritePath(db, Clock()).apply(Pull(), ["raw line with a secret"]) is False
    assert status(db, "claude.transcript")[3] == "TypeError at items[0]:str"


def test_success_clears_last_error(db, calls):
    wp = WritePath(db, Clock())
    wp.apply(Pull(), ["x"])
    wp.apply(Pull(), [])
    assert status(db, "claude.transcript")[3] is None


# --- the runtime --------------------------------------------------------------------

def make(tmp_path, sources, clock=None, **kw):
    return Runtime(sources, db_path=tmp_path / "usage.db", lock_path=tmp_path / "run.lock",
                   clock=clock or Clock(), **kw)


def test_watermarks_persist_and_resume(tmp_path, calls):
    clock = Clock()
    src = Pull(batches=[([state(1)], "wm1"), ([state(2)], "wm2")], interval_s=60)
    rt = make(tmp_path, [src], clock)
    rt.start()
    rt.run_once()
    rt.run_once()  # not due yet
    assert src.seen == [None]
    clock.t += 60
    rt.run_once()
    assert src.seen == [None, "wm1"]
    rt.stop()

    again = Pull(interval_s=60)
    rt2 = make(tmp_path, [again], clock)
    rt2.start()
    rt2.run_once()
    rt2.stop()
    assert again.seen == ["wm2"]


def test_failed_batch_does_not_advance_the_watermark(tmp_path, calls):
    clock = Clock()
    src = Pull(batches=[([state(1)], "wm1"), (["bad"], "wm2")], interval_s=1)
    rt = make(tmp_path, [src], clock)
    rt.start()
    rt.run_once()
    clock.t += 1
    rt.run_once()
    clock.t += 1
    rt.run_once()
    rt.stop()
    assert src.seen == [None, "wm1", "wm1"]


def test_a_failing_collect_is_reported_and_others_continue(tmp_path, calls):
    class Broken(Pull):
        def collect(self, watermark):
            raise OSError("/home/secret/file unreadable")

    good = Pull(name="good", batches=[([state(1)], "w")])
    rt = make(tmp_path, [Broken(name="broken"), good])
    rt.start()
    rt.run_once()
    conn = rt.conn
    assert status(conn, "broken")[3] == "OSError at collect"
    assert status(conn, "good")[3] is None and good.seen == [None]
    rt.stop()


def test_second_runtime_refused_while_lock_held(tmp_path, calls):
    rt = make(tmp_path, [])
    rt.start()
    with pytest.raises(Problem, match="already running"):
        make(tmp_path, []).start()
    rt.stop()
    rt2 = make(tmp_path, [])
    rt2.start()
    rt2.stop()


def test_push_items_written_on_runtime_thread(tmp_path, calls):
    push = Push()
    rt = make(tmp_path, [push])
    rt.start()
    t = threading.Thread(target=push.sink, args=([obs("p1"), obs("p2")],))
    t.start()
    t.join()
    assert calls.log == []  # nothing is written by the push source's thread
    rt.run_once()
    assert [c[1] for c in calls.log if c[0] == "upsert"] == ["p1", "p2"]
    assert set(calls.threads) == {threading.get_ident()}
    assert ("reconcile", 100, False) in calls.log
    rt.stop()
    assert push.stopped


def test_items_pushed_before_stop_are_drained(tmp_path, calls):
    push = Push()
    rt = make(tmp_path, [push])
    rt.start()
    push.sink([obs("late")])
    rt.stop()
    assert ("upsert", "late", None) in calls.log


def test_run_forever_until_stopped(tmp_path, calls):
    src = Pull(batches=[([state(1)], "w")])
    rt = make(tmp_path, [src])
    rt.start()
    stop = threading.Event()
    stop.set()
    rt.run_forever(stop)  # returns at once
    t = threading.Timer(0.3, stop.set)
    stop.clear()
    t.start()
    rt.run_forever(stop, tick_s=0.05)
    assert src.seen[0] is None
    rt.stop()


def test_runtime_row_and_twenty_past_runs(tmp_path, calls):
    clock = Clock()
    for _ in range(KEEP_PAST_RUNS + 3):
        clock.t += 1
        rt = make(tmp_path, [], clock)
        rt.start()
        current = rt.runtime_id
        rt.stop()
    conn = connect(tmp_path / "usage.db")
    rows = conn.execute("SELECT runtime_id, started_at, version FROM runtime ORDER BY started_at").fetchall()
    assert len(rows) == KEEP_PAST_RUNS + 1
    assert rows[-1][0] == current and rows[-1][1] == int(clock.t * 1000)
    assert rows[0][1] == int((clock.t - KEEP_PAST_RUNS) * 1000)
    conn.close()


def test_heartbeat_updates(tmp_path, calls):
    clock = Clock()
    rt = make(tmp_path, [], clock, heartbeat_s=0.02)
    rt.start()
    clock.t += 5
    conn = connect(tmp_path / "usage.db", readonly=True)
    deadline = time.monotonic() + 5
    while one(conn, "SELECT heartbeat_at FROM runtime")[0] != int(clock.t * 1000):
        assert time.monotonic() < deadline, "no heartbeat"
        time.sleep(0.02)
    conn.close()
    rt.stop()


def test_runtime_makes_no_network_calls(tmp_path, calls, monkeypatch):
    import socket

    def refuse(*a, **k):
        raise AssertionError("network call")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    rt = make(tmp_path, [Pull(batches=[([state(1)], "w")]), Push()])
    rt.start()
    rt.run_once()
    rt.stop()


# --- liveness ------------------------------------------------------------------------

def test_liveness_running_stalled_none(tmp_path, calls):
    clock = Clock()
    rt = make(tmp_path, [Pull(batches=[([state(1)], "w")])], clock)
    rt.start()
    rt.run_once()
    paths = dict(db_path=tmp_path / "usage.db", lock_path=tmp_path / "run.lock")

    live = liveness(**paths, now=clock.t + 5)
    assert live.state == "running" and live.heartbeat_age_s == 5
    assert live.data_as_of == int(T0 * 1000)
    at = time.strftime("%H:%M:%S", time.localtime(T0))
    assert live.message() == f"collector running, data as of {at}"

    stalled = liveness(**paths, now=clock.t + 30)
    assert stalled.state == "stalled"
    assert stalled.message() == (f"collector stalled (pid {rt_pid()}, last heartbeat {at}); "
                                 f"data as of {at}")

    rt.stop()
    gone = liveness(**paths, now=clock.t + 5)
    assert gone.state == "none"
    assert gone.message() == (f"no collector running; data as of {at}; "
                              "start one with: usage-watch run")


def rt_pid():
    import os
    return os.getpid()


def test_liveness_from_written_heartbeat_rows(tmp_path):
    import fcntl
    conn = connect(tmp_path / "usage.db")
    migrate(conn)
    now = T0
    conn.execute("INSERT INTO runtime VALUES (1, 42, ?, ?, '0.1.0')",
                 (int(now * 1000) - 100_000, int(now * 1000) - 29_000))
    lock = open(tmp_path / "run.lock", "a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    paths = dict(db_path=tmp_path / "usage.db", lock_path=tmp_path / "run.lock")
    assert liveness(**paths, now=now).state == "running"
    conn.execute("UPDATE runtime SET heartbeat_at = ?", (int(now * 1000) - 30_000,))
    live = liveness(**paths, now=now)
    assert live.state == "stalled" and live.pid == 42 and live.heartbeat_age_s == 30
    assert live.message().endswith("no data yet")
    lock.close()
    assert liveness(**paths, now=now).state == "none"
    conn.close()


def test_liveness_check_leaves_the_lock_free(tmp_path, calls):
    rt = make(tmp_path, [])
    rt.start()
    rt.stop()
    liveness(db_path=tmp_path / "usage.db", lock_path=tmp_path / "run.lock")
    rt2 = make(tmp_path, [])
    rt2.start()  # the reader's shared attempt was released
    rt2.stop()


def test_liveness_without_a_store(tmp_path):
    live = liveness(db_path=tmp_path / "none.db", lock_path=tmp_path / "run.lock", now=T0)
    assert live.state == "none"
    assert live.message() == "no collector running; no data yet; start one with: usage-watch run"
    assert not (tmp_path / "run.lock").exists()  # the reader creates nothing


def test_liveness_older_schema(tmp_path):
    conn = sqlite3.connect(tmp_path / "usage.db")
    conn.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER, migrated_at INTEGER)")
    conn.commit()
    conn.close()
    live = liveness(db_path=tmp_path / "usage.db", lock_path=tmp_path / "run.lock", now=T0)
    assert "the collector hasn't migrated yet" in live.message()


def test_core_imports_without_collaborators():
    # The write path looks its collaborators up per call, so core stands alone.
    assert core.PullSource and core.PushSource

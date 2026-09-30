"""End to end: real sources through the real runtime, write path, reconciler
and attribution, with the design's invariants checked on the stored result.
No fakes of usage-watch's own modules."""

import sqlite3
import threading

import pytest

from usage_watch import store
from usage_watch.model import AttributionEvidence, CapacitySample, Session, UsageObservation
from usage_watch.runtime import attribution
from usage_watch.runtime.core import Runtime
from usage_watch.runtime.liveness import liveness

T0 = 1_790_000_000_000
SK = "claude:sess-1"


def largest_output(old, new):
    """Claude's counting rule: keep the most complete streaming snapshot."""
    return new if (new.output_tokens or 0) >= (old.output_tokens or 0) else old


def obs(source, key, prk, out, *, t=T0, uncached=10, cache_read=100, cache_write=0):
    return UsageObservation(
        source=source, stream_key=SK, source_request_key=key, provider_request_key=prk,
        confidence="authoritative", observed_at=t, harness="claude", provider="anthropic",
        model="claude-opus-5-5", session_key=SK, uncached_input_tokens=uncached,
        cache_read_input_tokens=cache_read, cache_write_input_tokens=cache_write,
        output_tokens=out,
    )


class Pull:
    """A pull source that hands out one scripted batch per call."""

    def __init__(self, name, primary, batches, merge=None):
        self.name, self.primary, self.merge, self.interval_s = name, primary, merge, 0
        self.batches = list(batches)
        self.seen_watermarks = []

    def collect(self, watermark):
        self.seen_watermarks.append(watermark)
        if not self.batches:
            return [], None
        items = self.batches.pop(0)
        return items, f"pos-{len(self.seen_watermarks)}"


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "usage.db", tmp_path / "run.lock"


def totals(db):
    conn = sqlite3.connect(db)
    try:
        n, out = conn.execute("select count(*), sum(output_tokens) from usage_events").fetchone()
        return n, out
    finally:
        conn.close()


def test_collection_reconciles_links_and_attributes(paths):
    db, lock = paths
    session = Session(session_key=SK, harness="claude", session_id="sess-1", started_at=T0, live=True)
    transcript = Pull("claude.transcript", True, [
        # pass 1: a streaming snapshot, and an unrelated second request
        [session, obs("claude.transcript", "m1+r1", "prk-1", 2), obs("claude.transcript", "m2+r2", "prk-2", 50)],
        # pass 2: the completed snapshot of request 1 arrives in a later pass
        [obs("claude.transcript", "m1+r1", "prk-1", 12_402)],
    ], merge=largest_output)
    otel = Pull("claude.otel", False, [
        # arrives before its primary is complete, disagrees by 2, plus an orphan
        [obs("claude.otel", "r1", "prk-1", 12_400), obs("claude.otel", "r9", "prk-9", 999)],
        [],
    ])
    evidence = Pull("claude.state", False, [[AttributionEvidence(
        subject_kind="session", subject_id=SK, dimension="account", value="acct-a",
        method="otel_identity", source="claude.otel", confidence="authoritative",
        validity="historical", first_observed_at=T0, last_confirmed_at=T0)]], None)
    anchors = Pull("claude.statusline", False, [[CapacitySample(
        source="claude.statusline", stream_key=SK, window="session", confidence="authoritative",
        observed_at=T0, used_pct=4.0, resets_at=T0 + 3_600_000, status="ok")]])

    rt = Runtime([transcript, otel, evidence, anchors], db_path=db, lock_path=lock, heartbeat_s=0.05)
    rt.start()
    try:
        rt.run_once()
        rt.run_once()
        assert liveness(db_path=db, lock_path=lock).state == "running"
    finally:
        rt.stop()

    conn = sqlite3.connect(db)
    # invariant 5 and 10: one event per primary request, never one per snapshot
    n, out = totals(db)
    assert n == 2
    # streaming update advanced the same event in place, and the total is the final snapshot
    assert out == 12_402 + 50
    ev = conn.execute(
        "select e.usage_id, e.output_tokens, e.disagreement from usage_events e "
        "join usage_observations o on o.observation_id = e.accounting_observation_id "
        "where o.source_request_key = 'm1+r1'").fetchone()
    assert ev[1] == 12_402 and '"output_tokens": 2' in ev[2].replace('":2', '": 2')
    # the linked secondary supports the event; the unlinked one is an orphan and never counts (invariant 9)
    roles = conn.execute(
        "select o.source_request_key, eo.role from event_observations eo "
        "join usage_observations o on o.observation_id = eo.observation_id").fetchall()
    assert roles == [("r1", "supporting")]
    assert conn.execute("select count(*) from usage_observations where source_request_key='r9'").fetchone()[0] == 1
    # invariant 4: total input is derived and null-safe
    row = conn.execute("select uncached_input_tokens + cache_read_input_tokens + cache_write_input_tokens "
                       "from usage_events where usage_id = ?", (ev[0],)).fetchone()
    assert row[0] == 110
    # the anchor was stored; watermarks persisted per source
    assert conn.execute("select count(*) from capacity_samples").fetchone()[0] == 1
    assert conn.execute("select count(*) from watermarks").fetchone()[0] >= 2
    conn.close()

    # attribution: the event inherits its session's account at query time (invariant 1: counted once)
    conn = store.connect(db, readonly=True)
    eff = attribution.effective(conn, "usage_event", str(ev[0]), "account")
    assert eff.state == "attributed" and eff.value is not None
    conn.close()

    assert liveness(db_path=db, lock_path=lock).state == "none"


def test_second_runtime_is_refused_while_first_holds_the_lock(paths):
    db, lock = paths
    first = Runtime([], db_path=db, lock_path=lock, heartbeat_s=0.05)
    first.start()
    try:
        with pytest.raises(Exception, match="(?i)running|lock|held"):
            Runtime([], db_path=db, lock_path=lock).start()
    finally:
        first.stop()


def test_a_bad_batch_rolls_back_and_the_runtime_carries_on(paths):
    db, lock = paths
    bad = UsageObservation(source="claude.transcript", stream_key=SK, source_request_key="x",
                           confidence="nonsense", observed_at=T0)  # violates the CHECK
    src = Pull("claude.transcript", True, [[bad], [obs("claude.transcript", "ok", None, 7)]])
    rt = Runtime([src], db_path=db, lock_path=lock, heartbeat_s=0.05)
    rt.start()
    try:
        rt.run_once()
        rt.run_once()
    finally:
        rt.stop()
    n, out = totals(db)
    assert (n, out) == (1, 7)
    conn = sqlite3.connect(db)
    err = conn.execute("select last_error from collector_status where collector='claude.transcript'").fetchone()
    conn.close()
    assert err is None or "nonsense" not in (err[0] or "")  # D5: errors never carry values

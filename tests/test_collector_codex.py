"""The `codex.rollout` collector (C2): sessions, the delta rule, anchors, tailing.

Fixtures under fixtures/codex/ are synthetic: they mirror the key structure
of real rollout files, with no copied content."""

import json
import os
import shutil
import sqlite3

import pytest

from usage_watch import identity
from usage_watch.collectors import codex
from usage_watch.collectors.codex import CodexRolloutSource, window_name
from usage_watch.model import AttributionEvidence, CapacitySample, Session, UsageObservation
from usage_watch.runtime.core import Runtime

from fakes import FIXTURES

SECRET = b"c" * 32
A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
SKA, SKB = f"codex:{A}", f"codex:{B}"
T_A_LAST = 1788256980000  # 2026-09-01T10:03:00Z, the last record of A
T_B_LAST = 1788339610000  # 2026-09-02T09:00:10Z, the last record of B
LATE = 1_800_000_000.0     # long after every fixture record


def ms(iso):
    return codex._ms(iso)


class Clock:
    def __init__(self, t=LATE):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def root(tmp_path):
    dst = tmp_path / "sessions"
    shutil.copytree(FIXTURES / "codex" / "sessions", dst)
    return dst


def file_a(root):
    return next(root.rglob(f"*{A}.jsonl"))


def source(root, clock=None):
    return CodexRolloutSource(root=root, clock=clock or Clock(), secret=SECRET)


def of(items, kind):
    return [i for i in items if isinstance(i, kind)]


def token_line(ts, total, rate_limits=None, **fields):
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
             "reasoning_output_tokens": 0, "total_tokens": total, **fields}
    return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {
        "type": "token_count", "info": {"total_token_usage": usage, "last_token_usage": usage,
                                        "model_context_window": 1},
        "rate_limits": rate_limits}}, separators=(",", ":")) + "\n"


# --- the contract ---------------------------------------------------------------

def test_satisfies_the_pull_contract(tmp_path, monkeypatch):
    s = CodexRolloutSource(root=tmp_path)
    assert (s.name, s.primary, s.merge, s.interval_s) == ("codex.rollout", True, None, 30)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    assert CodexRolloutSource().root == tmp_path / "home" / "sessions"


def test_missing_root_yields_nothing(tmp_path):
    items, wm = source(tmp_path / "absent").collect(None)
    assert items == [] and json.loads(wm) == {"files": {}, "state": {}}


# --- sessions and branch ---------------------------------------------------------------

def test_sessions_come_from_each_files_first_session_meta(root):
    items, _ = source(root).collect(None)
    sessions = of(items, Session)
    assert {s.session_key for s in sessions} == {SKA, SKB}  # the fork's parent copy is ignored
    a_start = next(s for s in sessions if s.session_key == SKA and s.started_at is not None)
    assert (a_start.harness, a_start.session_id, a_start.cwd) == ("codex", A, "/work/demo")
    assert a_start.started_at == ms("2026-09-01T10:00:00.000Z")
    a_last = [s for s in sessions if s.session_key == SKA][-1]
    assert a_last.last_seen_at == T_A_LAST and a_last.live is False


def test_branch_evidence_is_historical_and_authoritative(root):
    ev = of(source(root).collect(None)[0], AttributionEvidence)
    assert {(e.subject_id, e.value) for e in ev} == {(SKA, "feature/x"), (SKB, "fork-branch")}
    assert all((e.subject_kind, e.dimension, e.validity, e.confidence, e.source) ==
               ("session", "branch", "historical", "authoritative", "codex.rollout") for e in ev)


def test_live_within_ten_minutes_then_not(root):
    clock = Clock(T_B_LAST / 1000 + 60)
    s = source(root, clock)
    items, wm = s.collect(None)
    last = {x.session_key: x for x in of(items, Session)}
    assert last[SKA].live is False and last[SKB].live is True
    clock.t += 10 * 60
    items, wm = s.collect(wm)
    assert [(x.session_key, x.live) for x in items] == [(SKB, False)]
    assert s.collect(wm)[0] == []  # said once


# --- the delta rule ---------------------------------------------------------------------

def test_delta_rule_on_the_fixture(root):
    s = source(root)
    obs = [o for o in of(s.collect(None)[0], UsageObservation) if o.session_key == SKA]
    got = [(o.source_request_key, o.model, o.uncached_input_tokens, o.cache_read_input_tokens,
            o.cache_write_input_tokens, o.output_tokens, o.reasoning_output_tokens) for o in obs]
    assert got == [
        ("2026-09-01T10:00:05.000Z|1100", "gpt-test-1", 200, 800, 0, 100, 40),
        ("2026-09-01T10:01:00.000Z|3300", "gpt-test-1", 300, 1700, 0, 200, 60),
        # 10:02 decreased: a new baseline, nothing emitted
        ("2026-09-01T10:03:00.000Z|780", "gpt-test-2", 100, 100, 0, 30, 10),
    ]
    o = obs[0]
    assert (o.source, o.stream_key, o.confidence, o.provider, o.harness, o.provider_request_key,
            o.auxiliary, o.observed_at) == (
        "codex.rollout", SKA, "authoritative", "openai", "codex", None, False,
        ms("2026-09-01T10:00:05.000Z"))
    assert json.loads(o.native) == {"input_tokens": 1000, "cached_input_tokens": 800,
                                    "cache_write_input_tokens": 0, "output_tokens": 100,
                                    "reasoning_output_tokens": 40, "total_tokens": 1100}
    assert (s.stats["info_null"], s.stats["repeats"], s.stats["decreases"]) == (1, 1, 1)


def test_absent_cache_write_reads_as_zero(root):
    obs = [o for o in of(source(root).collect(None)[0], UsageObservation) if o.session_key == SKB]
    assert len(obs) == 1
    assert (obs[0].uncached_input_tokens, obs[0].cache_write_input_tokens) == (300, 0)
    assert "cache_write_input_tokens" not in json.loads(obs[0].native)


def test_totals_restart_in_a_continuation_file(root):
    # A session continued in a second file restarts its totals (seen in real
    # data): the first event there is usage, not a decrease.
    cont = root / "2026" / "09" / "03" / "rollout-2026-09-03T00-00-00-33333333-3333-4333-8333-333333333333.jsonl"
    cont.parent.mkdir(parents=True)
    meta = file_a(root).read_text().splitlines()[0]
    cont.write_text(meta + "\n" + token_line("2026-09-03T00:00:01.000Z", 60, input_tokens=50,
                                             output_tokens=10))
    obs = [o for o in of(source(root).collect(None)[0], UsageObservation)
           if o.source_request_key.startswith("2026-09-03")]
    assert [(o.session_key, o.uncached_input_tokens, o.output_tokens) for o in obs] == [(SKA, 50, 10)]


def test_without_a_session_meta_the_stream_is_a_hashed_path(tmp_path):
    f = tmp_path / "rollout-x.jsonl"
    f.write_text(token_line("2026-09-01T00:00:00.000Z", 10, input_tokens=10))
    items, _ = source(tmp_path).collect(None)
    [o] = items
    assert o.session_key is None and o.stream_key == identity.stream(str(f), SECRET)


# --- anchors ------------------------------------------------------------------------------

def test_window_names_by_length():
    assert [window_name(s) for s in (18000, 604800, 3600)] == ["session", "weekly", "other:3600s"]


def test_anchors_only_on_change(root):
    caps = [c for c in of(source(root).collect(None)[0], CapacitySample) if c.stream_key == SKA]
    assert [(c.window, c.used_pct, c.window_seconds, c.observed_at) for c in caps] == [
        ("session", 10.0, 18000, ms("2026-09-01T10:00:02.000Z")),   # from an info:null event
        ("weekly", 40.0, 604800, ms("2026-09-01T10:00:02.000Z")),
        ("session", 12.0, 18000, ms("2026-09-01T10:01:00.000Z")),
        ("weekly", 41.0, 604800, ms("2026-09-01T10:02:00.000Z")),   # a 10080-minute primary
    ]
    assert caps[0].resets_at == 1788229200 * 1000
    assert all((c.source, c.confidence, c.status) == ("codex.rollout", "authoritative", "unknown")
               for c in caps)


def test_no_account_evidence(root):
    ev = of(source(root).collect(None)[0], AttributionEvidence)
    assert all(e.dimension != "account" for e in ev)


# --- privacy ------------------------------------------------------------------------------

def test_no_content_reaches_an_item(root):
    items, wm = source(root).collect(None)
    assert "SYNTHETIC-CONTENT" not in repr(items) and "SYNTHETIC-CONTENT" not in wm
    assert "example.invalid" not in repr(items) and "synthetic" not in repr(items)


# --- tailing and watermarks -----------------------------------------------------------------

def test_a_second_pass_reads_nothing_new(root):
    s = source(root)
    _, wm = s.collect(None)
    assert s.collect(wm) == ([], wm)
    files = json.loads(wm)["files"]
    assert files[str(file_a(root))] == [os.stat(file_a(root)).st_ino, file_a(root).stat().st_size]


def test_appended_events_continue_the_delta_from_the_watermark(root):
    s = source(root)
    _, wm = s.collect(None)
    with open(file_a(root), "a") as fh:
        fh.write(token_line("2026-09-01T10:04:00.000Z", 1000, input_tokens=900,
                            cached_input_tokens=150, cache_write_input_tokens=0,
                            output_tokens=100, reasoning_output_tokens=20))
    items, wm = s.collect(wm)
    [o] = of(items, UsageObservation)
    assert (o.model, o.uncached_input_tokens, o.cache_read_input_tokens, o.output_tokens,
            o.reasoning_output_tokens) == ("gpt-test-2", 150, 50, 20, 10)
    assert [x.session_key for x in of(items, Session)] == [SKA]


def test_an_incomplete_final_line_waits(root):
    s = source(root)
    _, wm = s.collect(None)
    line = token_line("2026-09-01T10:04:00.000Z", 900, input_tokens=800, cached_input_tokens=100,
                      cache_write_input_tokens=0, output_tokens=100, reasoning_output_tokens=10)
    size = file_a(root).stat().st_size
    with open(file_a(root), "a") as fh:
        fh.write(line[:40])
    items, wm = s.collect(wm)
    assert of(items, UsageObservation) == []
    assert json.loads(wm)["files"][str(file_a(root))][1] == size
    with open(file_a(root), "a") as fh:
        fh.write(line[40:])
    items, _ = s.collect(wm)
    assert [o.source_request_key for o in of(items, UsageObservation)] == ["2026-09-01T10:04:00.000Z|900"]


def test_a_shorter_file_or_new_inode_starts_over(root):
    s = source(root)
    _, wm = s.collect(None)
    f = file_a(root)
    lines = f.read_text().splitlines(keepends=True)
    f.unlink()
    f.write_text("".join(lines[:5]))  # a new inode, and shorter
    items, _ = s.collect(wm)
    assert [o.source_request_key for o in of(items, UsageObservation)] == ["2026-09-01T10:00:05.000Z|1100"]
    assert of(items, Session)[0].started_at is not None


def test_an_unreadable_watermark_starts_over(root):
    first, _ = source(root).collect(None)
    again, _ = source(root).collect("not json")
    assert len(again) == len(first)


def test_deleted_files_leave_the_watermark(root):
    s = source(root)
    _, wm = s.collect(None)
    file_a(root).unlink()
    _, wm = s.collect(wm)
    assert list(json.loads(wm)["files"]) == [str(next(root.rglob(f"*{B}.jsonl")))]


# --- through the real runtime -------------------------------------------------------------------

def test_through_the_runtime_into_a_store(root, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    db = tmp_path / "usage.db"
    s = CodexRolloutSource(root=root, interval_s=0, clock=Clock(), secret=SECRET)
    rt = Runtime([s], db_path=db, lock_path=tmp_path / "run.lock")
    rt.start()
    try:
        rt.run_once()
        with open(file_a(root), "a") as fh:
            fh.write(token_line("2026-09-01T10:04:00.000Z", 1000, input_tokens=900,
                                cached_input_tokens=150, output_tokens=100,
                                reasoning_output_tokens=20))
        rt.run_once()
        rt.run_once()
    finally:
        rt.stop()
    conn = sqlite3.connect(db)
    try:
        q = lambda sql: conn.execute(sql).fetchall()
        assert q("select collector, last_error from collector_status") == [("codex.rollout", None)]
        assert q("select count(*), sum(output_tokens), sum(uncached_input_tokens) from usage_events") == [
            (5, 100 + 200 + 30 + 20 + 20, 200 + 300 + 100 + 150 + 300)]
        assert q("select count(*) from usage_observations") == [(5,)]
        assert sorted(q("select session_key, session_id, cwd, live from sessions")) == [
            (SKA, A, "/work/demo", 0), (SKB, B, "/work/demo-fork", 0)]
        assert q("select started_at from sessions where session_key = 'codex:" + A + "'") == [
            (ms("2026-09-01T10:00:00.000Z"),)]
        assert q("select count(*) from capacity_samples") == [(6,)]
        assert sorted(q("select subject_id, value, state from effective_attributions"
                        " where dimension = 'branch'")) == [
            (SKA, "feature/x", "attributed"), (SKB, "fork-branch", "attributed")]
    finally:
        conn.close()


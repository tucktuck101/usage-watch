"""The Claude Code collectors (C1, C2): `claude.transcript` and
`claude.cached_utilization`. Fixtures are synthetic and mirror the real key
structure; no real content."""

import datetime as dt
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from usage_watch import identity, store
from usage_watch.collectors import claude
from usage_watch.collectors.claude import (
    STREAM,
    ClaudeCachedUtilizationSource, ClaudeTranscriptSource, keep_larger_output,
)
from usage_watch.model import (
    AttributionEvidence, CapacitySample, LimitEvent, Session, UsageObservation,
)
from usage_watch.runtime import attribution
from usage_watch.runtime.core import Runtime, account_alias_value

FIX = Path(__file__).parent / "fixtures" / "claude"
SECRET = b"c" * 32
SID = "11111111-1111-4111-8111-111111111111"
SK = f"claude:{SID}"
UTC = dt.timezone.utc


def ms(s: str) -> int:
    return int(dt.datetime.fromisoformat(s).timestamp() * 1000)


LAST_LINE = ms("2026-09-30T15:32:00+00:00")


class Clock:
    def __init__(self, t_ms: int):
        self.t = t_ms / 1000

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s


@pytest.fixture
def home(tmp_path):
    shutil.copytree(FIX / "projects", tmp_path / "projects")
    shutil.copy(FIX / "claude.json", tmp_path / "claude.json")
    return tmp_path


def transcript(home, clock, **kw):
    return ClaudeTranscriptSource(root=home / "projects", state_file=home / "claude.json",
                                  clock=clock, secret=SECRET, tz=UTC, **kw)


def of(items, cls):
    return [i for i in items if isinstance(i, cls)]


def alias(acct, org):
    return account_alias_value("anthropic", "anthropic.account_org",
                               identity.account("anthropic", f"{acct}|{org}".lower(), SECRET))


OWNER = alias("AAAAAAAA-0000-4000-8000-000000000001", "BBBBBBBB-0000-4000-8000-000000000002")
LOGIN = alias("aaaaaaaa-0000-4000-8000-000000000001", "bbbbbbbb-0000-4000-8000-000000000002")


# --- observations ------------------------------------------------------------------------

def test_observations_collapse_snapshots_and_skip_synthetic_and_null_ids(home):
    items, _ = transcript(home, Clock(LAST_LINE + 86_400_000)).collect(None)
    obs = {o.source_request_key: o for o in of(items, UsageObservation)}
    assert sorted(obs) == ["msg_A+req_A", "msg_B+req_B", "msg_C+req_C"]

    a = obs["msg_A+req_A"]
    assert a.output_tokens == 120 and a.reasoning_output_tokens == 40  # the final snapshot
    assert a.observed_at == ms("2026-09-30T10:00:02+00:00")
    assert (a.uncached_input_tokens, a.cache_read_input_tokens, a.cache_write_input_tokens) == (10, 3000, 200)
    assert a.stream_key == STREAM and a.session_key == SK
    assert a.provider_request_key == identity.request("anthropic", "req_A", SECRET)
    assert (a.source, a.harness, a.provider, a.model, a.confidence) == (
        "claude.transcript", "claude", "anthropic", "claude-test-1", "authoritative")
    assert json.loads(a.native) == {
        "input_tokens": 10, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 3000,
        "output_tokens": 120, "output_tokens_details.thinking_tokens": 40}

    assert obs["msg_B+req_B"].reasoning_output_tokens is None  # no details: null, never 0
    # a subagent's lines carry the parent's sessionId, so they join its stream
    assert obs["msg_C+req_C"].stream_key == STREAM and obs["msg_C+req_C"].reasoning_output_tokens == 0


def test_merge_keeps_the_larger_output():
    base = dict(source="s", stream_key="k", source_request_key="r", confidence="authoritative")
    small = UsageObservation(observed_at=1, output_tokens=2, **base)
    big = UsageObservation(observed_at=2, output_tokens=9, **base)
    unknown = UsageObservation(observed_at=3, output_tokens=None, **base)
    assert keep_larger_output(small, big) == big
    assert keep_larger_output(big, small) is big
    assert keep_larger_output(big, unknown) is big
    assert keep_larger_output(unknown, small) == small
    assert ClaudeTranscriptSource(root="/nonexistent").merge(small, big) == big


def test_merge_keeps_the_first_session_of_a_request():
    base = dict(source="s", stream_key="k", source_request_key="r", confidence="authoritative")
    first = UsageObservation(observed_at=1, output_tokens=2, session_key="claude:a", **base)
    copy = UsageObservation(observed_at=2, output_tokens=9, session_key="claude:b", **base)
    merged = keep_larger_output(first, copy)
    assert merged.output_tokens == 9 and merged.session_key == "claude:a"


# --- sessions and evidence ------------------------------------------------------------------

def test_session_from_timestamps_and_not_live_when_old(home):
    items, _ = transcript(home, Clock(LAST_LINE + 86_400_000)).collect(None)
    (s,) = of(items, Session)
    assert s.session_key == SK and s.session_id == SID and s.harness == "claude"
    assert s.started_at == ms("2026-09-30T10:00:00+00:00")
    assert s.last_seen_at == LAST_LINE
    assert s.cwd == "/tmp/demo"  # the session's own file, not the subagent's
    assert s.live is False
    ev = of(items, AttributionEvidence)
    assert not [e for e in ev if e.method == "state_file"]  # never for a session that isn't live


def test_branch_and_owner_evidence(home):
    items, _ = transcript(home, Clock(LAST_LINE + 86_400_000)).collect(None)
    ev = of(items, AttributionEvidence)
    branches = [(e.value, e.valid_from) for e in ev if e.dimension == "branch"]
    assert branches == [("main", ms("2026-09-30T10:00:00+00:00")),
                        ("feature/x", ms("2026-09-30T10:01:00+00:00"))]
    assert all(e.validity == "historical" and e.confidence == "authoritative"
               for e in ev if e.dimension == "branch")
    (owner,) = [e for e in ev if e.method == "transcript_owner"]
    assert (owner.value, owner.validity, owner.confidence, owner.subject_id) == (
        OWNER, "historical", "authoritative", SK)


def test_live_session_gets_state_file_evidence_then_goes_quiet(home):
    clock = Clock(LAST_LINE + 60_000)
    src = transcript(home, clock)
    items, wm = src.collect(None)
    (s,) = of(items, Session)
    assert s.live is True
    (login,) = [e for e in of(items, AttributionEvidence) if e.method == "state_file"]
    now = LAST_LINE + 60_000
    assert (login.value, login.validity, login.confidence) == (LOGIN, "live", "inferred")
    assert login.first_observed_at == login.last_confirmed_at == login.valid_from == now

    clock.tick(60)
    items, wm = src.collect(wm)
    assert of(items, Session) == []  # nothing new in the files
    (login2,) = of(items, AttributionEvidence)
    assert login2.valid_from == now and login2.last_confirmed_at == now + 60_000  # same row, confirmed

    clock.tick(15 * 60)
    items, wm = src.collect(wm)
    assert [(i.session_key, i.live) for i in items] == [(SK, False)]


def test_restart_clears_live_on_quiet_sessions(home):
    _, wm = transcript(home, Clock(LAST_LINE + 60_000)).collect(None)
    main = home / "projects" / "-tmp-demo" / f"{SID}.jsonl"
    old = (LAST_LINE - 3_600_000) / 1000
    os.utime(main, (old, old))
    items, _ = transcript(home, Clock(LAST_LINE + 3_600_000)).collect(wm)  # a new process
    assert [(i.session_key, i.live) for i in of(items, Session)] == [(SK, False)]


# --- limit events -------------------------------------------------------------------------------

def test_limit_events_from_rate_limit_records_and_notices(home):
    items, _ = transcript(home, Clock(LAST_LINE + 86_400_000)).collect(None)
    ev = {e.source_key: e for e in of(items, LimitEvent)}
    assert sorted(ev) == ["a-4", "s-1", "s-2"]
    hit = ev["a-4"]
    assert (hit.kind, hit.window, hit.resets_at, hit.stream_key, hit.confidence) == (
        "hit", "session", ms("2026-09-30T15:30:00+00:00"), SK, "authoritative")
    assert hit.observed_at == ms("2026-09-30T13:59:00+00:00")
    notice = ev["s-1"]
    assert (notice.kind, notice.window) == ("hit", None)
    assert notice.resets_at == ms("2026-09-30T15:30:00+00:00")  # "at 3:30 pm", local (UTC here)
    reset = ev["s-2"]
    assert (reset.kind, reset.resets_at) == ("reset", None)


@pytest.mark.parametrize("text,kind,window,at", [
    ("usage limit reached · continuing automatically at 9 pm · x", "hit", None, "2026-09-30T21:00"),
    ("usage limit reached · continuing automatically at 1:05 am · x", "hit", None, "2026-10-01T01:05"),
    ("You've hit your session limit · resets 3pm", "hit", "session", "2026-09-30T15:00"),
    ("usage limit reset · continuing automatically", "reset", None, None),
    ("something else entirely", None, None, None),
])
def test_notice_classification(text, kind, window, at):
    src = ClaudeTranscriptSource(root="/nonexistent", secret=SECRET, tz=UTC)
    out: list = []
    ts = ms("2026-09-30T14:00:00+00:00")
    src._notice({"uuid": "u", "content": text}, SK, ts, out)
    if kind is None:
        assert out == []
        return
    (e,) = out
    assert (e.kind, e.window) == (kind, window)
    assert e.resets_at == (ms(at + ":00+00:00") if at else None)


# --- tailing ----------------------------------------------------------------------------------------

def append(path: Path, text: str):
    with open(path, "a") as fh:
        fh.write(text)


def assistant_line(uuid, mid, rid, out, ts="2026-09-30T16:00:00.000Z"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": SID, "timestamp": ts, "cwd": "/tmp/demo",
        "gitBranch": "feature/x", "requestId": rid,
        "message": {"id": mid, "model": "claude-test-1", "role": "assistant", "content": [],
                    "usage": {"input_tokens": 1, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0, "output_tokens": out}}})


def test_tail_reads_only_new_complete_lines(home):
    main = home / "projects" / "-tmp-demo" / f"{SID}.jsonl"
    src = transcript(home, Clock(LAST_LINE + 86_400_000))
    _, wm = src.collect(None)
    marks = json.loads(wm)
    assert marks[str(main)] == [main.stat().st_ino, main.stat().st_size]

    items, wm2 = src.collect(wm)
    assert items == [] and wm2 == wm  # nothing new, nothing emitted

    line = assistant_line("a-9", "msg_D", "req_D", 5)
    append(main, line[:40])  # an incomplete final line is not consumed
    items, wm3 = src.collect(wm2)
    assert of(items, UsageObservation) == [] and json.loads(wm3)[str(main)][1] == marks[str(main)][1]

    append(main, line[40:] + "\n" + assistant_line("a-10", "msg_A", "req_A", 500) + "\n")
    items, wm4 = src.collect(wm3)
    obs = {o.source_request_key: o for o in of(items, UsageObservation)}
    assert sorted(obs) == ["msg_A+req_A", "msg_D+req_D"]  # a later snapshot of A re-sent for merging
    assert obs["msg_A+req_A"].output_tokens == 500
    assert json.loads(wm4)[str(main)][1] == main.stat().st_size
    (s,) = of(items, Session)
    assert s.started_at is None  # not the start of the file: never overwrite the stored start
    assert not [e for e in of(items, AttributionEvidence) if e.dimension == "branch"]  # unchanged


def test_new_inode_or_shorter_file_restarts_at_zero(home):
    main = home / "projects" / "-tmp-demo" / f"{SID}.jsonl"
    src = transcript(home, Clock(LAST_LINE + 86_400_000))
    _, wm = src.collect(None)
    content = main.read_bytes()
    main.unlink()
    main.write_bytes(content[: content.index(b"\n") + 1])  # a new, shorter file
    items, wm2 = src.collect(wm)
    assert json.loads(wm2)[str(main)] == [main.stat().st_ino, main.stat().st_size]
    (s,) = of(items, Session)
    assert s.session_key == SK

    main.write_bytes(content)  # same inode, rewritten
    items, _ = src.collect(wm2)
    assert len(of(items, UsageObservation)) == 2


def test_bad_watermark_and_missing_root_are_safe(home, tmp_path):
    src = ClaudeTranscriptSource(root=tmp_path / "none", secret=SECRET)
    assert src.collect("not json") == ([], "{}")
    items, _ = transcript(home, Clock(LAST_LINE)).collect('{"x": "y"}')
    assert len(of(items, UsageObservation)) == 3


def test_malformed_lines_are_skipped(home):
    main = home / "projects" / "-tmp-demo" / f"{SID}.jsonl"
    append(main, "{not json\n[1, 2]\n\n")
    items, _ = transcript(home, Clock(LAST_LINE + 86_400_000)).collect(None)
    assert len(of(items, UsageObservation)) == 3


# --- cached utilization ------------------------------------------------------------------------------

def cached(home, **kw):
    return ClaudeCachedUtilizationSource(path=home / "claude.json", clock=Clock(1_790_000_100_000),
                                         secret=SECRET, **kw)


def test_cached_utilization_windows_and_account(home):
    items, wm = cached(home).collect(None)
    samples = {s.window: s for s in of(items, CapacitySample)}
    assert sorted(samples) == ["session", "weekly", "weekly:sonnet"]  # null and non-model keys dropped
    s = samples["session"]
    assert (s.used_pct, s.window_seconds, s.status, s.confidence, s.observed_at) == (
        42.0, 18000, "unknown", "observed", 1_790_000_000_000)
    assert s.resets_at == ms("2026-09-22T18:00:00.123+00:00")
    assert s.stream_key == identity.stream(str(home / "claude.json"), SECRET)
    assert s.source == "claude.cached_utilization"
    assert samples["weekly"].window_seconds == 604800 and samples["weekly:sonnet"].used_pct == 12.5
    assert samples["weekly:sonnet"].resets_at is None
    assert wm == "1790000000000"

    ev = of(items, AttributionEvidence)
    assert len(ev) == 3
    for e in ev:
        assert (e.subject_kind, e.dimension, e.value, e.method, e.confidence, e.validity) == (
            "capacity_sample", "account", LOGIN, "state_file", "observed", "historical")
    assert {e.subject_id for e in ev} == {
        f"claude.cached_utilization|{s.stream_key}|{w}|1790000000000" for w in samples}


def test_cached_utilization_emits_nothing_until_fetched_changes(home):
    src = cached(home)
    _, wm = src.collect(None)
    assert src.collect(wm) == ([], wm)
    data = json.loads((home / "claude.json").read_text())
    data["cachedUsageUtilization"]["fetchedAtMs"] += 1000
    (home / "claude.json").write_text(json.dumps(data))
    items, wm2 = src.collect(wm)
    assert wm2 == "1790000001000" and len(of(items, CapacitySample)) == 3


def test_cached_utilization_for_another_login_has_no_account(home):
    data = json.loads((home / "claude.json").read_text())
    data["cachedUsageUtilization"]["accountUuid"] = "cccccccc-0000-4000-8000-000000000003"
    (home / "claude.json").write_text(json.dumps(data))
    items, _ = cached(home).collect(None)
    assert len(of(items, CapacitySample)) == 3 and of(items, AttributionEvidence) == []


def test_cached_utilization_missing_file(tmp_path):
    src = ClaudeCachedUtilizationSource(path=tmp_path / "none.json", secret=SECRET)
    assert src.collect("5") == ([], "5")


# --- through the real runtime ---------------------------------------------------------------------------

def test_integration_through_runtime(home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    db, lock = tmp_path / "usage.db", tmp_path / "run.lock"
    clock = Clock(LAST_LINE + 60_000)  # the session is live
    sources = [
        ClaudeTranscriptSource(root=home / "projects", state_file=home / "claude.json", clock=clock, tz=UTC),
        ClaudeCachedUtilizationSource(path=home / "claude.json", clock=clock),
    ]
    rt = Runtime(sources, db_path=db, lock_path=lock, clock=clock, heartbeat_s=0.05)
    rt.start()
    try:
        rt.run_once()
        main = home / "projects" / "-tmp-demo" / f"{SID}.jsonl"
        append(main, assistant_line("a-11", "msg_A", "req_A", 900) + "\n")
        clock.tick(301)
        rt.run_once()
    finally:
        rt.stop()

    conn = sqlite3.connect(db)
    q = lambda sql: conn.execute(sql).fetchall()
    assert q("select collector, last_error from collector_status order by 1") == [
        ("claude.cached_utilization", None), ("claude.transcript", None)]
    # one event per request; the later snapshot advanced A in place
    assert q("select count(*), sum(output_tokens) from usage_events") == [(3, 900 + 7 + 33)]
    assert q("select count(*) from usage_observations") == [(3,)]
    assert q("select session_key, live, started_at from sessions") == [
        (SK, 1, ms("2026-09-30T10:00:00+00:00"))]
    assert q("select kind, count(*) from limit_events group by kind") == [("hit", 2), ("reset", 1)]
    assert q("select window from capacity_samples order by 1") == [
        ("session",), ("weekly",), ("weekly:sonnet",)]
    # branch: the second value closed the first
    assert q("select value, valid_to is not null from attribution_evidence"
             " where dimension='branch' order by valid_from") == [("main", 1), ("feature/x", 0)]
    conn.close()

    conn = store.connect(db, readonly=True)
    try:
        # the transcript owner (authoritative) outranks the current login (inferred)
        eff = attribution.effective(conn, "session", SK, "account")
        assert eff.state == "attributed" and eff.confidence == "authoritative"
        sid = conn.execute("select source || '|' || stream_key || '|' || window || '|' || observed_at"
                           " from capacity_samples where window='session'").fetchone()[0]
        eff = attribution.effective(conn, "capacity_sample", sid, "account")
        assert eff.state == "attributed" and eff.confidence == "observed"

        # D5: nothing forbidden reached the store
        dump = "\n".join(conn.iterdump()).lower()
        for secret in ("aaaaaaaa-0000", "bbbbbbbb-0000", "example.invalid", "synthetic-content",
                       "synthetic-prompt", "synthetic tail", "synthetic-title", "synthetic-user-id",
                       "continuing automatically"):
            assert secret not in dump, secret
    finally:
        conn.close()

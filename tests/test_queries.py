"""Queries (F4, V1, V2) over a store built through the real write path:
the runtime, the reconciler and attribution, with scripted sources."""

import datetime as dt

import pytest

from usage_watch import queries, store
from usage_watch.model import (AgentStateSample, AttributionEvidence, CapacitySample, Checkout,
                               Session, UsageObservation)
from usage_watch.runtime.core import Runtime, account_alias_value

NOW = 1_790_000_000.0          # epoch seconds
NOW_MS = int(NOW * 1000)
HOUR = 3_600_000
ACCT_A = account_alias_value("anthropic", "anthropic.account_org", "a" * 16)
ACCT_B = account_alias_value("anthropic", "anthropic.account_org", "b" * 16)
ACCT_C = account_alias_value("anthropic", "anthropic.account_org", "c" * 16)


class Pull:
    def __init__(self, name, primary, batches):
        self.name, self.primary, self.merge, self.interval_s = name, primary, None, 0
        self.batches = list(batches)

    def collect(self, watermark):
        return (self.batches.pop(0) if self.batches else []), None


def obs(source, sk, key, t, *, harness="claude", provider="anthropic", model="claude-opus-5-5",
        uncached=10, cache_read=100, cache_write=5, output=50, aux=False, prk=None):
    return UsageObservation(
        source=source, stream_key=sk or "nosession", source_request_key=key, provider_request_key=prk,
        confidence="authoritative", observed_at=t, harness=harness, provider=provider, model=model,
        session_key=sk, uncached_input_tokens=uncached, cache_read_input_tokens=cache_read,
        cache_write_input_tokens=cache_write, output_tokens=output, auxiliary=aux)


def ev(kind, subject, dim, value, method="record", t=NOW_MS - 10 * HOUR, validity="historical",
       confidence="authoritative", last=None):
    return AttributionEvidence(
        subject_kind=kind, subject_id=subject, dimension=dim, value=value, method=method,
        source="test", confidence=confidence, validity=validity, first_observed_at=t,
        last_confirmed_at=last if last is not None else t)


def cap(source, stream, window, t, used, resets=None):
    return CapacitySample(source=source, stream_key=stream, window=window, confidence="authoritative",
                          observed_at=t, used_pct=used, resets_at=resets, status="ok")


def build_store(db, lock):
    """A store with three sessions, one per attribution outcome, written
    through the real runtime. Returns nothing; tests read it back."""
    t = NOW_MS - 2 * HOUR
    sessions = [
        Session(session_key="claude:s1", harness="claude", session_id="s1", started_at=t,
                cwd="/work/proj-a/sub"),
        Session(session_key="claude:s2", harness="claude", session_id="s2", started_at=t,
                cwd="/work/proj-b"),
        Session(session_key="codex:s3", harness="codex", session_id="s3", started_at=t,
                cwd="/elsewhere"),
    ]
    checkouts = [Checkout(checkout_id="c1", display_name="proj-a", local_path="/work/proj-a"),
                 Checkout(checkout_id="c2", display_name="proj-b", local_path="/work/proj-b"),
                 Checkout(checkout_id="c0", display_name="proj", local_path="/work/proj")]
    claude = Pull("claude.transcript", True, [[
        *sessions,
        obs("claude.transcript", "claude:s1", "r1", t),                       # usage_id 1
        obs("claude.transcript", "claude:s1", "r2", t + 1, cache_write=None),  # an unknown component
        obs("claude.transcript", "claude:s1", "r3", t + 2, aux=True, output=7),
        obs("claude.transcript", "claude:s2", "r4", t + 3, model="claude-sonnet-5"),
        obs("claude.transcript", "claude:s2", "r5", t + 4, model="claude-sonnet-5"),
        obs("claude.transcript", None, "r6", t + 5),                          # no session
        obs("claude.transcript", "claude:s1", "old", NOW_MS - 48 * HOUR),      # outside 24h
    ]])
    codex = Pull("codex.rollout", True, [[
        obs("codex.rollout", "codex:s3", "c1", t, harness="codex", provider="openai", model="gpt-6"),
        obs("codex.rollout", "codex:s3", "c2", t + 1, harness="codex", provider="openai", model="gpt-6"),
    ]])
    otel = Pull("claude.otel", False, [[obs("claude.otel", "claude:s1", "orphan", t, prk="nomatch",
                                            output=99_999)]])
    facts = Pull("facts", False, [[
        *checkouts,
        ev("session", "claude:s1", "account", ACCT_A),
        ev("session", "claude:s2", "account", ACCT_B, method="m1"),
        ev("session", "claude:s2", "account", ACCT_C, method="m2"),
        ev("session", "claude:s1", "branch", "main"),
        ev("usage_event", "1", "branch", "feature"),   # the event's own evidence beats its session's
        # capacity: two readings on one stream (newest wins), one unattributed source, one stale
        cap("claude.statusline", "claude:s1", "session", NOW_MS - 120_000, 40.0, NOW_MS + HOUR),
        cap("claude.statusline", "claude:s1", "session", NOW_MS - 60_000, 42.0, NOW_MS + HOUR),
        ev("capacity_sample", f"claude.statusline|claude:s1|session|{NOW_MS - 60_000}", "account", ACCT_A,
           t=NOW_MS - 60_000),
        ev("capacity_sample", f"claude.statusline|claude:s1|session|{NOW_MS - 120_000}", "account", ACCT_A,
           t=NOW_MS - 120_000),
        cap("codex.rollout", "codex:s3", "weekly", NOW_MS - 5 * 60_000, 96.0),
        cap("claude.cached_utilization", "home", "weekly", NOW_MS - 2 * HOUR, 10.0),
        ev("capacity_sample", f"claude.cached_utilization|home|weekly|{NOW_MS - 2 * HOUR}", "account",
           ACCT_A, t=NOW_MS - 2 * HOUR),
        # agents: %1 is current and joined to s1 by live pane evidence; %2 is too old to show
        AgentStateSample(pane="%1", observed_at=NOW_MS - 10_000, state="stalled", source="screen",
                         harness="claude", model="Opus 5.5"),
        AgentStateSample(pane="%1", observed_at=NOW_MS - 20_000, state="busy", source="screen"),
        AgentStateSample(pane="%2", observed_at=NOW_MS - 5 * 60_000, state="busy", source="screen"),
        ev("session", "claude:s1", "pane", "%1", method="claude_session_file", validity="live",
           t=NOW_MS - 60_000, last=NOW_MS - 5_000),
    ]])
    rt = Runtime([claude, codex, otel, facts], db_path=db, lock_path=lock, heartbeat_s=60)
    rt.start()
    try:
        rt.run_once()
    finally:
        rt.stop()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = tmp_path / "state" / "usage-watch" / "usage.db"
    build_store(path, path.parent / "run.lock")
    return path


@pytest.fixture
def conn(db):
    c = store.connect(db, readonly=True)
    yield c
    c.close()


SINCE = NOW_MS - 24 * HOUR
SUMMED = ("requests", "total_input_tokens", "unknown_total_input", "unknown_requests",
          *queries.TOKEN_FIELDS)


def by_label(result):
    return {r["label"]: r for r in result["rows"]}


@pytest.mark.parametrize("by", queries.BY)
@pytest.mark.parametrize("aux", [True, False])
def test_the_total_is_the_sum_of_the_rows(conn, by, aux):
    """D1 invariants 1 and 2: every counting event appears once, so any
    grouping adds up to the same total."""
    result = queries.usage(conn, SINCE, None, by, include_auxiliary=aux)
    total = result["total"]
    for field in SUMMED:
        assert sum(r[field] for r in result["rows"]) == total[field], field
    for field in queries.TOKEN_FIELDS:
        assert sum(r["unknown"][field] for r in result["rows"]) == total["unknown"][field]
    assert total["requests"] == (8 if aux else 7)
    reference = queries.usage(conn, SINCE, None, "harness", include_auxiliary=aux)["total"]
    assert total == reference  # grouping never changes the total


def test_auxiliary_calls_are_inside_the_total(conn):
    """D1 invariant 3: total = primary + auxiliary; --no-auxiliary is a named filter."""
    full = queries.usage(conn, SINCE, None, "model")["total"]
    without = queries.usage(conn, SINCE, None, "model", include_auxiliary=False)["total"]
    assert full["output_tokens"] - without["output_tokens"] == 7
    assert full["requests"] - without["requests"] == 1


def test_unknowns_are_counted_never_zero(conn):
    total = queries.usage(conn, SINCE, None, "harness")["total"]
    assert total["unknown_requests"] == 1
    assert total["unknown"]["cache_write_input_tokens"] == 1
    assert total["unknown_total_input"] == 1
    # the known sums leave the unknown out rather than adding a 0 for it
    assert total["cache_write_input_tokens"] == 5 * 7
    assert total["total_input_tokens"] == 115 * 7
    assert total["uncached_input_tokens"] == 10 * 8


def test_orphans_and_old_events_never_count(conn):
    total = queries.usage(conn, SINCE, None, "session")["total"]
    assert total["output_tokens"] < 99_999       # the unlinked secondary observation (invariant 9)
    everything = queries.usage(conn, None, None, "session")["total"]
    assert everything["requests"] == total["requests"] + 1


def test_grouping_by_model_provider_and_harness_comes_from_the_accounting_observation(conn):
    rows = by_label(queries.usage(conn, SINCE, None, "provider"))
    assert rows["anthropic"]["requests"] == 6 and rows["openai"]["requests"] == 2
    rows = by_label(queries.usage(conn, SINCE, None, "model"))
    assert rows["claude-sonnet-5"]["requests"] == 2 and rows["gpt-6"]["requests"] == 2
    rows = by_label(queries.usage(conn, SINCE, None, "harness"))
    assert rows["claude"]["requests"] == 6 and rows["codex"]["requests"] == 2


def test_account_uses_effective_attribution_with_inheritance(conn):
    result = queries.usage(conn, SINCE, None, "account")
    rows = {(r["state"], r["label"]): r["requests"] for r in result["rows"]}
    attributed = [(k, n) for k, n in rows.items() if k[0] == "attributed"]
    assert len(attributed) == 1
    (_, label), n = attributed[0]
    assert label.startswith("anthropic account ") and len(label.split()[-1]) == 6
    assert n == 3                                   # s1's three events inherit its account
    assert rows[("ambiguous", "(ambiguous)")] == 2  # s2: disagreeing authoritative accounts
    assert rows[("unattributed", "(unattributed)")] == 3  # codex and the event with no session
    shares = result["shares"]
    assert shares["attributed"] == pytest.approx(3 / 8) and shares["unattributed"] == pytest.approx(3 / 8)


def test_branch_prefers_the_events_own_evidence(conn):
    rows = {(r["state"], r["key"]): r["requests"] for r in queries.usage(conn, SINCE, None, "branch")["rows"]}
    assert rows[("attributed", "feature")] == 1
    assert rows[("attributed", "main")] == 2
    assert rows[("unattributed", None)] == 5


def test_project_maps_the_session_cwd_to_a_checkout(conn):
    rows = {(r["state"], r["key"]): r["requests"] for r in queries.usage(conn, SINCE, None, "project")["rows"]}
    # /work/proj-a/sub is under proj-a, never under /work/proj (a prefix on a path boundary only)
    assert rows == {("attributed", "proj-a"): 3, ("attributed", "proj-b"): 2, ("unattributed", None): 3}


def test_day_groups_by_local_day(conn):
    rows = queries.usage(conn, SINCE, None, "day")["rows"]
    days = {dt.datetime.fromtimestamp((NOW_MS - 2 * HOUR) / 1000).date().isoformat()}
    assert {r["key"] for r in rows} <= days | {dt.datetime.fromtimestamp(NOW).date().isoformat()}


def test_pools_newest_per_account_and_window(conn):
    pools = queries.pools(conn, NOW)
    attributed = [p for p in pools if p["account"]]
    session = next(p for p in attributed if p["window"] == "session")
    assert session["used_pct"] == 42.0 and session["remaining_pct"] == 58.0
    assert session["age"] == "1m" and session["stale"] is False
    weekly = next(p for p in attributed if p["window"] == "weekly")
    assert weekly["source"] == "claude.cached_utilization" and weekly["stale"] is True  # 2 h > 60 min
    codex = next(p for p in pools if p["source"] == "codex.rollout")
    assert codex["account"] is None and codex["account_label"] == "unattributed (codex.rollout)"
    assert codex["stale"] is False and codex["remaining_pct"] == 4.0
    assert session["account_label"] == weekly["account_label"]


def test_agents_join_pane_to_session_and_account(conn):
    agents = queries.agents(conn, NOW)
    assert [a["pane"] for a in agents] == ["%1"]
    one = agents[0]
    assert one["state"] == "stalled" and one["session_key"] == "claude:s1"
    assert one["session_join"] == "pane_evidence"
    assert one["account_state"] == "attributed" and one["account_label"].startswith("anthropic account")


# --- "since I last looked" (V2) -------------------------------------------------------

@pytest.fixture
def rw(db):
    c = store.connect(db)
    yield c
    c.close()


def test_a_normal_close_is_the_marker(conn, rw):
    assert queries.since_last_look(conn, "cli", "usage").since_ms is None
    queries.open_look(rw, "cli", "usage", 1000)
    queries.touch_look(rw, "cli", "usage", 1500)
    queries.touch_look(rw, "cli", "usage", 1800)   # a refresh never moves the marker
    queries.close_look(rw, "cli", "usage", 2000)
    last = queries.since_last_look(conn, "cli", "usage")
    assert (last.since_ms, last.basis, last.flagged) == (2000, "closed_at", False)
    assert last.note() is None


def test_an_abnormal_exit_falls_back_to_last_seen_and_says_so(conn, rw):
    queries.open_look(rw, "dashboard", "dashboard", 1000)
    queries.touch_look(rw, "dashboard", "dashboard", 1500)
    # no close: the process died
    last = queries.since_last_look(conn, "dashboard", "dashboard")
    assert (last.since_ms, last.basis, last.flagged) == (1500, "last_seen_at", True)
    assert "without closing" in last.note()
    # a new look clears the old close, so a crash in it is detected too
    queries.close_look(rw, "dashboard", "dashboard", 1600)
    queries.open_look(rw, "dashboard", "dashboard", 3000)
    last = queries.since_last_look(conn, "dashboard", "dashboard")
    assert (last.since_ms, last.basis, last.flagged) == (3000, "opened_at", True)


def test_views_are_independent(conn, rw):
    queries.open_look(rw, "cli", "usage", 1000)
    queries.close_look(rw, "cli", "usage", 1100)
    queries.open_look(rw, "dashboard", "dashboard", 5000)
    queries.touch_look(rw, "dashboard", "dashboard", 5100)
    queries.close_look(rw, "dashboard", "dashboard", 5200)
    assert queries.since_last_look(conn, "cli", "usage").since_ms == 1100
    assert queries.since_last_look(conn, "dashboard", "dashboard").since_ms == 5200
    rows = conn.execute("SELECT who, \"view\", opened_at, closed_at FROM looks ORDER BY who").fetchall()
    assert rows == [("cli", "usage", 1000, 1100), ("dashboard", "dashboard", 5000, 5200)]


def test_parse_since():
    assert queries.parse_since("24h", NOW) == NOW_MS - 24 * HOUR
    assert queries.parse_since("7d", NOW) == NOW_MS - 7 * 24 * HOUR
    assert queries.parse_since("2026-10-01T00:00:00Z", NOW) == int(
        dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
    with pytest.raises(ValueError):
        queries.parse_since("yesterday-ish", NOW)

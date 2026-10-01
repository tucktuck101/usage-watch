"""D8 nudge policy over a migrated store: store states built directly, screen faked."""

import json

import pytest

from usage_watch import config, policy
from usage_watch.adapters.base import Reading
from usage_watch.errors import Problem
from usage_watch.store import connect, migrate

NOW_S = 1_800_000_000
NOW = NOW_S * 1000
SEC, MIN, HOUR = 1000, 60_000, 3_600_000
ACCT = "a" * 32
OTHER = "b" * 32
SK = "omp:s1"
PANE = "%1"


class FakeAdapter:
    """read(plain) returns the Reading the test put on the screen."""

    def __init__(self, screens):
        self.screens = screens

    def read(self, plain, styled=""):
        return self.screens[plain]


@pytest.fixture
def env(tmp_path, monkeypatch):
    conn = connect(tmp_path / "usage.db")
    migrate(conn)
    screens = {PANE: Reading("stalled", model="Fable 5.1", error_key="E1")}
    typed, notes = [], []
    monkeypatch.setattr(policy.screen, "capture", lambda pane: (pane, ""))
    monkeypatch.setattr(policy.screen, "type_into", lambda pane, text: typed.append((pane, text)))
    monkeypatch.setattr(policy.adapters, "by_name", lambda name: FakeAdapter(screens))

    def no_tmux():
        raise Problem("no tmux in tests", "none")
    monkeypatch.setattr(policy.topology, "scan", no_tmux)

    class Env:
        pass
    e = Env()
    e.conn, e.screens, e.typed, e.notes = conn, screens, typed, notes
    e.cfg = config.Config(nudge="continue")
    e.clock = [NOW_S]

    def make(act=True, settle_s=0):
        return policy.Policy(conn, e.cfg, act=act, clock=lambda: e.clock[0],
                             notify=lambda t, m: notes.append(m), settle_s=settle_s)
    e.policy = make
    yield e
    conn.close()


# --- store builders ------------------------------------------------------------------------

def sample(conn, at, state="stalled", error_key="E1", pane=PANE, harness="omp",
           model="Fable 5.1"):
    conn.execute("INSERT INTO state_samples (pane, observed_at, harness, state, model, error_key,"
                 " source) VALUES (?, ?, ?, ?, ?, ?, 'screen')",
                 (pane, at, harness, state, model, error_key if state == "stalled" else None))


def join(conn, sk=SK, pane=PANE, last=NOW - 10 * SEC):
    conn.execute("INSERT INTO attribution_evidence (subject_kind, subject_id, dimension, value,"
                 " method, source, confidence, validity, first_observed_at, last_confirmed_at)"
                 " VALUES ('session', ?, 'pane', ?, 'tmux', 'panes', 'authoritative', 'live',"
                 " ?, ?)", (sk, pane, last - HOUR, last))


def effective(conn, kind, subject, value=ACCT, state="attributed", confidence="authoritative"):
    conn.execute("INSERT INTO effective_attributions (subject_kind, subject_id, dimension, state,"
                 " value, confidence) VALUES (?, ?, 'account', ?, ?, ?)",
                 (kind, subject, state, value if state == "attributed" else None, confidence))


def account(conn, key=ACCT, provider="anthropic", label="team"):
    conn.execute("INSERT INTO accounts (account_key, provider, label, first_seen, last_seen)"
                 " VALUES (?, ?, ?, 0, 0)", (key, provider, label))


def anchor(conn, window, used, *, source="omp.usage_cache", stream="st", at=NOW - MIN,
           resets=NOW + HOUR, status="ok", acct=ACCT):
    conn.execute("INSERT INTO capacity_samples (source, stream_key, \"window\", used_pct,"
                 " resets_at, status, confidence, observed_at) VALUES (?, ?, ?, ?, ?, ?,"
                 " 'observed', ?)", (source, stream, window, used, resets, status, at))
    if acct:
        effective(conn, "capacity_sample", f"{source}|{stream}|{window}|{at}", acct)


def limit(conn, kind, at, *, window=None, resets=None, sk=SK, key=None, source="claude.transcript",
          acct=None):
    key = key or f"k{at}{kind}"
    conn.execute("INSERT INTO limit_events (source, stream_key, source_key, \"window\", kind,"
                 " resets_at, confidence, observed_at) VALUES (?, ?, ?, ?, ?, ?, 'authoritative',"
                 " ?)", (source, sk, key, window, kind, resets, at))
    if acct:
        effective(conn, "limit_event", f"{source}|{sk}|{key}", acct)


def world(conn, *, blocking="session"):
    """Every D8 condition holds: a fresh stall, an authoritative account, a fresh
    complete report with both windows clear, and (by default) a transcript hit
    naming `session` at onset."""
    account(conn)
    sample(conn, NOW - 60 * SEC, "busy")
    sample(conn, NOW - 20 * SEC)                    # onset
    sample(conn, NOW - 5 * SEC)
    join(conn)
    effective(conn, "session", SK)
    anchor(conn, "session", 40)
    anchor(conn, "weekly", 30, resets=NOW + 3 * 24 * HOUR)
    if blocking:
        limit(conn, "hit", NOW - 22 * SEC, window=blocking)


def rows(conn):
    return conn.execute("SELECT stall_id, action, reason_code, count FROM nudges"
                        " ORDER BY nudge_id").fetchall()


def only(decisions):
    assert len(decisions) == 1
    return decisions[0]


# --- all four hold -------------------------------------------------------------------------

@pytest.mark.parametrize("blocking", ["session", None])
def test_nudges_only_when_all_four_hold(env, blocking):
    world(env.conn, blocking=blocking)
    d = only(env.policy().tick())
    assert (d.action, d.account, d.account_label, d.session_key) == ("nudge", ACCT, "team", SK)
    assert "all four conditions hold" in d.reason
    assert env.typed == [(PANE, "continue")]
    assert rows(env.conn) == [(d.stall_id, "nudge", "all-conditions-hold", 1)]
    ev = json.loads(env.conn.execute("SELECT evidence FROM nudges").fetchone()[0])
    assert ev["account"] == ACCT and ev["account_attribution"]["confidence"] == "authoritative"
    first = ev["anchor"] if blocking else ev["anchors"]["session"]
    assert first["source"] == "omp.usage_cache" and first["age_s"] == 60
    assert first["confidence"] == "observed"


def test_not_stalled_is_none(env):
    world(env.conn)
    sample(env.conn, NOW - 1 * SEC, "busy")
    d = only(env.policy().tick())
    assert d.action == "none" and d.stall_id is None and env.typed == [] and rows(env.conn) == []


# --- each condition failing alone ------------------------------------------------------------

def test_condition_1_stale_state_sample(env):
    world(env.conn)
    env.clock[0] = NOW_S + 30  # newest sample now 35 s old
    d = only(env.policy().tick())
    assert d.action == "wait" and d.reason.startswith("condition 1") and env.typed == []


def test_condition_1_no_error_key(env):
    world(env.conn)
    env.conn.execute("UPDATE state_samples SET error_key = NULL")
    d = only(env.policy().tick())
    assert d.action == "wait" and d.reason_code == "c1-no-error-key"


@pytest.mark.parametrize("state", ["unattributed", "ambiguous"])
def test_condition_2_account_not_attributed(env, state):
    world(env.conn)
    env.conn.execute("UPDATE effective_attributions SET state = ?, value = NULL"
                     " WHERE subject_kind = 'session'", (state,))
    d = only(env.policy().tick())
    assert d.action == "wait" and d.reason == f"condition 2: the session's account is {state}"
    assert env.typed == []


def test_condition_2_no_session_joined(env):
    world(env.conn)
    env.conn.execute("UPDATE attribution_evidence SET last_confirmed_at = ?", (NOW - 61 * SEC,))
    d = only(env.policy().tick())
    assert d.action == "wait" and d.session_key is None
    assert "condition 2: account unknown" in d.reason


def test_condition_2_inferred_needs_complete_discovery_and_one_account(env):
    world(env.conn)
    env.conn.execute("UPDATE effective_attributions SET confidence = 'inferred'"
                     " WHERE subject_kind = 'session'")
    # omp's discovery is complete, but the registry has no omp login for anthropic
    d = only(env.policy().tick())
    assert d.action == "wait" and d.reason_code == "c2-inferred-not-single"
    env.conn.execute("INSERT INTO account_aliases (alias_kind, alias_hash, account_key,"
                     " asserted_by, evidence, confidence, first_seen, last_confirmed) VALUES"
                     " ('omp.identity_key', 'h1', ?, 'omp.session', 'reported',"
                     " 'authoritative', 0, 0)", (ACCT,))
    d = only(env.policy().tick())
    assert d.action == "nudge"


def test_condition_2_inferred_on_partial_harness_waits(env):
    world(env.conn)
    env.conn.execute("UPDATE state_samples SET harness = 'claude'")
    env.conn.execute("UPDATE effective_attributions SET confidence = 'inferred'"
                     " WHERE subject_kind = 'session'")
    d = only(env.policy().tick())
    assert d.action == "wait" and d.reason_code == "c2-inferred-partial-discovery"


def test_condition_3_blocking_window_not_clear(env):
    world(env.conn)
    anchor(env.conn, "session", 97, at=NOW - 30 * SEC)
    d = only(env.policy().tick())
    assert d.action == "wait" and d.reason.startswith("condition 3: blocking window session")
    assert "3% remaining" in d.reason


def test_condition_3_exhausted_status_is_not_clear(env):
    world(env.conn)
    anchor(env.conn, "session", 50, at=NOW - 30 * SEC, status="exhausted")
    d = only(env.policy().tick())
    assert d.action == "wait" and d.reason_code == "c3-blocking-not-clear"


def test_unnamed_stall_never_defaults_to_session(env):
    world(env.conn, blocking=None)
    anchor(env.conn, "weekly", 99, at=NOW - 30 * SEC, resets=NOW + 3 * 24 * HOUR)
    d = only(env.policy().tick())
    assert d.action == "wait" and d.reason_code == "c3-window-unknown-not-all-clear"
    assert d.evidence["blocking_window"] is None


def test_condition_4_other_window_not_clear(env):
    world(env.conn)
    anchor(env.conn, "weekly", 98, at=NOW - 30 * SEC, resets=NOW + 3 * 24 * HOUR)
    d = only(env.policy().tick())
    assert d.action == "wait" and d.reason.startswith("condition 4: weekly has 2% remaining")


def test_condition_4_partial_discovery_means_set_unknown(env):
    world(env.conn)
    env.conn.execute("UPDATE capacity_samples SET source = 'claude.statusline'")
    env.conn.execute("UPDATE effective_attributions SET subject_id = replace(subject_id,"
                     " 'omp.usage_cache', 'claude.statusline')")
    d = only(env.policy().tick())
    assert d.action == "wait" and d.reason_code == "c4-window-set-unknown"


def test_condition_4_window_seen_only_in_ended_instance(env):
    world(env.conn)
    anchor(env.conn, "weekly:fable", 10, source="claude.statusline", stream="x",
           at=NOW - 2 * 24 * HOUR, resets=NOW - HOUR)
    d = only(env.policy().tick())
    assert d.action == "wait" and "weekly:fable" in d.reason and "current instance" in d.reason


def test_other_model_window_is_not_applicable(env):
    world(env.conn)
    anchor(env.conn, "weekly:opus", 100, stream="other", at=NOW - MIN)
    assert only(env.policy().tick()).action == "nudge"


# --- evidence kinds and freshness ---------------------------------------------------------

def test_estimate_and_usage_history_never_used(env):
    world(env.conn)
    env.conn.execute("DELETE FROM capacity_samples")
    # a fresh, generous reading from a source with control age "never", and one
    # from a source with no declared control age, authorise nothing
    for source in ("omp.usage_history", "estimate"):
        anchor(env.conn, "session", 0, source=source)
        anchor(env.conn, "weekly", 0, source=source, resets=NOW + 3 * 24 * HOUR)
    d = only(env.policy().tick())
    assert d.action == "wait" and env.typed == []


def test_stale_anchor_by_control_age_waits(env):
    world(env.conn)
    # 11 minutes: within omp.usage_cache's display age (15), past its control age (10)
    env.conn.execute("DELETE FROM capacity_samples")
    env.conn.execute("DELETE FROM effective_attributions WHERE subject_kind = 'capacity_sample'")
    anchor(env.conn, "session", 10, at=NOW - 11 * MIN)
    anchor(env.conn, "weekly", 10, at=NOW - 11 * MIN, resets=NOW + 3 * 24 * HOUR)
    d = only(env.policy().tick())
    assert d.action == "wait" and "control age" in d.reason


# --- recovery signals ------------------------------------------------------------------------

def recovery_world(conn):
    """The session window's newest anchor is current but stale, so (a) fails;
    the complete report is fresh for weekly; the session window is still known."""
    world(conn)
    conn.execute("DELETE FROM capacity_samples")
    conn.execute("DELETE FROM effective_attributions WHERE subject_kind = 'capacity_sample'")
    anchor(conn, "weekly", 30, source="claude.cached_utilization", stream="home",
           at=NOW - 5 * MIN, resets=NOW + 3 * 24 * HOUR)
    anchor(conn, "session", 30, source="claude.statusline", stream="sl", at=NOW - 20 * MIN,
           resets=NOW + 4 * HOUR)


def test_linked_passed_resets_at_clears_blocking_window(env):
    recovery_world(env.conn)
    assert only(env.policy().tick()).reason_code == "c3-blocking-not-clear"
    # the transcript hit for this stall, observed before onset, resets just now
    limit(env.conn, "hit", NOW - 25 * SEC, window="session", resets=NOW - 2 * SEC, acct=ACCT)
    d = only(env.policy().tick())
    assert d.action == "nudge" and d.evidence["signal"]["kind"] == "limit_event_resets_at"


def test_resets_at_from_another_account_or_earlier_stall_does_not_link(env):
    recovery_world(env.conn)
    limit(env.conn, "hit", NOW - 25 * SEC, window="session", resets=NOW - 2 * SEC, acct=OTHER)
    limit(env.conn, "hit", NOW - 10 * HOUR, window="session", resets=NOW - 5 * HOUR, acct=ACCT)
    assert only(env.policy().tick()).action == "wait"


def test_resets_at_not_yet_passed_does_not_clear(env):
    recovery_world(env.conn)
    limit(env.conn, "hit", NOW - 25 * SEC, window="session", resets=NOW + MIN, acct=ACCT)
    assert only(env.policy().tick()).action == "wait"


def test_reset_notice_after_onset_clears(env):
    recovery_world(env.conn)
    limit(env.conn, "reset", NOW - 30 * SEC)          # before onset: an earlier stall's
    assert only(env.policy().tick()).action == "wait"
    limit(env.conn, "reset", NOW - 3 * SEC)
    d = only(env.policy().tick())
    assert d.action == "nudge" and d.evidence["signal"]["kind"] == "reset_notice"


def test_recovery_signal_never_applies_to_unknown_window(env):
    recovery_world(env.conn)
    env.conn.execute("DELETE FROM limit_events")
    limit(env.conn, "reset", NOW - 3 * SEC)
    assert only(env.policy().tick()).reason_code == "c3-window-unknown-not-all-clear"


# --- acting ------------------------------------------------------------------------------------

@pytest.mark.parametrize("reading", [
    Reading("typing", model="Fable 5.1"),
    Reading("busy"),
    Reading("stalled", model="Fable 5.1", error_key="E2"),
])
def test_reread_aborts_when_pane_changed(env, reading):
    world(env.conn)
    env.screens[PANE] = reading
    d = only(env.policy().tick())
    assert d.action == "wait" and d.reason_code == "c1-reread-changed" and env.typed == []


def test_same_stall_never_nudged_twice_and_escalates_once(env):
    world(env.conn)
    p = env.policy()
    first = only(p.tick())
    assert first.action == "nudge"
    env.clock[0] += 1
    d = only(p.tick())                          # no sample since the nudge yet
    assert d.action == "wait" and d.reason_code == "nudged-settling"
    sample(env.conn, NOW + 3 * SEC)               # still the same stall after the nudge
    env.clock[0] += 4
    for _ in range(3):
        d = only(p.tick())
        assert d.action == "escalate" and d.stall_id == first.stall_id
    assert env.typed == [(PANE, "continue")]
    assert len(env.notes) == 1 and "persists after its nudge" in env.notes[0]
    assert [r[1:] for r in rows(env.conn)] == [
        ("nudge", "all-conditions-hold", 1), ("wait", "nudged-settling", 1),
        ("escalate", "stall-persisted-after-nudge", 3)]


def test_new_stall_after_busy_is_a_new_occurrence(env):
    world(env.conn)
    p = env.policy()
    first = only(p.tick())
    sample(env.conn, NOW + 2 * SEC, "busy")
    sample(env.conn, NOW + 10 * SEC)
    env.clock[0] += 12
    d = only(p.tick())
    assert d.action == "nudge" and d.stall_id != first.stall_id
    assert len(env.typed) == 2


def test_strikes_escalate(env):
    env.cfg.max_strikes = 1
    world(env.conn)
    p = env.policy()
    assert only(p.tick()).action == "nudge"
    sample(env.conn, NOW + 2 * SEC, "idle")           # not busy: the strike stands
    sample(env.conn, NOW + 10 * SEC)
    env.clock[0] += 12
    d = only(p.tick())
    assert d.action == "escalate" and d.reason_code == "strikes"
    assert len(env.typed) == 1 and len(env.notes) == 1


def test_waits_are_deduplicated(env):
    world(env.conn)
    env.conn.execute("UPDATE effective_attributions SET state = 'ambiguous', value = NULL"
                     " WHERE subject_kind = 'session'")
    p = env.policy()
    for i in range(3):
        env.clock[0] = NOW_S + i
        p.tick()
    assert [r[1:] for r in rows(env.conn)] == [("wait", "c2-ambiguous", 3)]
    last = env.conn.execute("SELECT decided_at, last_seen_at FROM nudges").fetchone()
    assert last == (NOW, NOW + 2 * SEC)
    # a different reason writes a new row
    env.conn.execute("UPDATE effective_attributions SET state = 'unattributed'"
                     " WHERE subject_kind = 'session'")
    p.tick()
    assert [r[2] for r in rows(env.conn)] == ["c2-ambiguous", "c2-unattributed"]


def test_act_false_types_nothing_and_writes_nothing(env):
    world(env.conn)
    d = only(env.policy(act=False).tick())
    assert d.action == "nudge" and "nothing typed" in d.reason
    assert env.typed == [] and rows(env.conn) == []


def test_older_schema_waits_without_reading(env):
    env.conn.execute("UPDATE schema_version SET version = 0")
    p = env.policy()
    assert p.tick() == [] and "hasn't migrated" in p.schema_message


def test_codex_pane_without_session_join_waits(env):
    world(env.conn)
    env.conn.execute("DELETE FROM attribution_evidence")
    env.conn.execute("UPDATE state_samples SET harness = 'codex', model = 'gpt-5.1-codex'")
    d = only(env.policy().tick())
    assert d.action == "wait" and "account unknown" in d.reason


def test_window_matching():
    assert policy.family_word("Fable 5.1") == "fable"
    assert policy.family_word("claude-opus-4-5") == "opus"
    assert policy.applicable("weekly:fable", "Fable 5.1")
    assert not policy.applicable("weekly:opus", "Fable 5.1")
    assert policy.applicable("weekly:opus", None)
    assert policy.applicable("other:x", "Fable 5.1") and policy.applicable("session", None)


def test_account_at_follows_the_login_in_use_at_that_time(tmp_path):
    from usage_watch import store
    from usage_watch.model import AttributionEvidence
    from usage_watch.policy import account_at
    from usage_watch.runtime import accounts, attribution
    conn = store.connect(tmp_path / "u.db")
    store.migrate(conn)
    a = accounts.create_account(conn, "anthropic")
    b = accounts.create_account(conn, "anthropic")

    def ev(value, t):
        return AttributionEvidence(
            subject_kind="session", subject_id="omp:s", dimension="account", value=value,
            method="credential_id", source="omp.session", confidence="authoritative",
            validity="historical", first_observed_at=t, last_confirmed_at=t, valid_from=t)

    for value, t in ((a, 100), (b, 200), (a, 300)):
        attribution.add_evidence(conn, ev(value, t))
    assert account_at(conn, "omp:s", 150).value == a
    assert account_at(conn, "omp:s", 250).value == b
    assert account_at(conn, "omp:s", 999).value == a
    assert account_at(conn, "omp:s", 50).state == "unattributed"

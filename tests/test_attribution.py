import pytest

from usage_watch.model import AttributionEvidence
from usage_watch.runtime import accounts, attribution
from usage_watch.store import connect, migrate


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "usage.db")
    migrate(conn)
    yield conn
    conn.close()


def ev(value, *, subject="claude:s1", kind="session", dim="branch", method="record",
       confidence="authoritative", validity="historical", valid_from=0, valid_to=None,
       first=100, last=100):
    return AttributionEvidence(
        subject_kind=kind, subject_id=subject, dimension=dim, value=value, method=method,
        source="test", confidence=confidence, validity=validity, first_observed_at=first,
        last_confirmed_at=last, valid_from=valid_from, valid_to=valid_to)


def rows(conn, sql="SELECT value, valid_from, valid_to, first_observed_at, last_confirmed_at"
                   " FROM attribution_evidence ORDER BY evidence_id"):
    return conn.execute(sql).fetchall()


def add_event(conn, session_key):
    obs = conn.execute(
        "INSERT INTO usage_observations (source, stream_key, source_request_key, confidence,"
        " observed_at) VALUES ('claude.transcript', 's', 'r1', 'authoritative', 500)").lastrowid
    return conn.execute(
        "INSERT INTO usage_events (accounting_observation_id, observed_at, session_key,"
        " reconciled_version) VALUES (?, 500, ?, 1)", (obs, session_key)).lastrowid


# --- evidence ----------------------------------------------------------------

def test_upsert_and_reconfirm(db):
    first = attribution.add_evidence(db, ev("main", first=100, last=100))
    again = attribution.add_evidence(db, ev("main", first=300, last=300))
    assert first == again
    assert rows(db) == [("main", 0, None, 100, 300)]
    # an older sighting never moves last_confirmed_at backwards
    attribution.add_evidence(db, ev("main", first=50, last=50))
    assert rows(db) == [("main", 0, None, 100, 300)]


def test_value_change_closes_valid_to(db):
    attribution.add_evidence(db, ev("main", method="login", validity="time_bounded",
                                    valid_from=100, first=100, last=100))
    attribution.add_evidence(db, ev("dev", method="login", validity="time_bounded",
                                    valid_from=400, first=450, last=450))
    assert rows(db) == [("main", 100, 400, 100, 100), ("dev", 400, None, 450, 450)]
    # unbounded start: closed at the new evidence's first_observed_at
    attribution.add_evidence(db, ev("x", method="m2", first=10, last=10))
    attribution.add_evidence(db, ev("y", method="m2", first=20, last=20))
    assert rows(db)[2:] == [("x", 0, 20, 10, 10), ("y", 0, None, 20, 20)]


def test_another_method_is_not_closed(db):
    attribution.add_evidence(db, ev("main", method="a"))
    attribution.add_evidence(db, ev("dev", method="b"))
    assert [r[2] for r in rows(db)] == [None, None]


# --- resolution ----------------------------------------------------------------

def test_highest_confidence_wins(db):
    attribution.add_evidence(db, ev("main", method="a", confidence="inferred"))
    e = attribution.add_evidence(db, ev("dev", method="b", confidence="authoritative"))
    r = attribution.resolve(db, "session", "claude:s1", "branch", 200)
    assert (r.state, r.value, r.confidence, r.evidence_id) == ("attributed", "dev", "authoritative", e)


def test_tie_with_different_values_is_ambiguous(db):
    attribution.add_evidence(db, ev("main", method="a"))
    attribution.add_evidence(db, ev("dev", method="b"))
    r = attribution.resolve(db, "session", "claude:s1", "branch", 200)
    assert (r.state, r.value, r.evidence_id) == ("ambiguous", None, None)
    assert r.confidence == "authoritative" and r.note


def test_lower_confidence_does_not_break_a_tie(db):
    attribution.add_evidence(db, ev("main", method="a", confidence="observed"))
    attribution.add_evidence(db, ev("dev", method="b", confidence="observed"))
    attribution.add_evidence(db, ev("main", method="c", confidence="inferred"))
    attribution.add_evidence(db, ev("main", method="d", confidence="inferred"))
    r = attribution.resolve(db, "session", "claude:s1", "branch", 200)
    assert (r.state, r.value, r.confidence) == ("ambiguous", None, "observed")


def test_agreeing_values_at_top_are_attributed(db):
    attribution.add_evidence(db, ev("main", method="a"))
    attribution.add_evidence(db, ev("main", method="b"))
    assert attribution.resolve(db, "session", "claude:s1", "branch", 200).state == "attributed"


def test_evidence_outside_window_is_ignored(db):
    # authoritative but time-bounded to [100, 400]; inferred live over [500, 900]
    attribution.add_evidence(db, ev("old", method="a", validity="time_bounded",
                                    valid_from=100, valid_to=400, first=100, last=100))
    attribution.add_evidence(db, ev("live", method="b", confidence="inferred", validity="live",
                                    valid_from=500, first=500, last=900))
    at = lambda t: attribution.resolve(db, "session", "claude:s1", "branch", t)
    assert (at(300).state, at(300).value) == ("attributed", "old")
    assert (at(600).state, at(600).value) == ("attributed", "live")
    r = at(1000)  # the live evidence was last confirmed at 900
    assert (r.state, r.value, r.evidence_id) == ("unattributed", None, None)
    assert r.note


def test_one_effective_row_per_subject_and_dimension(db):
    attribution.add_evidence(db, ev("main", method="a"))
    attribution.resolve(db, "session", "claude:s1", "branch", 200)
    attribution.add_evidence(db, ev("dev", method="b"))
    attribution.resolve(db, "session", "claude:s1", "branch", 200)
    attribution.add_evidence(db, ev("t", dim="task", method="a"))
    attribution.resolve(db, "session", "claude:s1", "task", 200)
    got = db.execute("SELECT dimension, state FROM effective_attributions ORDER BY dimension").fetchall()
    assert got == [("branch", "ambiguous"), ("task", "attributed")]


def test_account_values_compare_as_canonical_accounts(db):
    a = accounts.create_account(db, "anthropic")
    b = accounts.create_account(db, "anthropic")
    attribution.add_evidence(db, ev(a, dim="account", method="otel"))
    attribution.add_evidence(db, ev(b, dim="account", method="transcript"))
    assert attribution.resolve(db, "session", "claude:s1", "account", 200).state == "ambiguous"
    merge_id = accounts.merge(db, a, b, evidence="user", verified_by=None)
    r = attribution.resolve(db, "session", "claude:s1", "account", 200)
    assert (r.state, r.value) == ("attributed", b)
    accounts.unmerge(db, merge_id)
    assert attribution.resolve(db, "session", "claude:s1", "account", 200).state == "ambiguous"


# --- failed attempts and inheritance -----------------------------------------------

def test_failed_attempt_row(db):
    attribution.record_failed_attempt(db, "session", "codex:s9", "account",
                                      "account-unknown:codex-no-otel")
    r = attribution.effective(db, "session", "codex:s9", "account")
    assert (r.state, r.value, r.evidence_id, r.note) == (
        "unattributed", None, None, "account-unknown:codex-no-otel")
    # not written when evidence exists
    attribution.add_evidence(db, ev("main", subject="claude:s2"))
    attribution.record_failed_attempt(db, "session", "claude:s2", "branch", "nope")
    assert db.execute("SELECT count(*) FROM effective_attributions"
                      " WHERE subject_id = 'claude:s2'").fetchone()[0] == 0


def test_resolve_with_no_evidence_stores_nothing(db):
    r = attribution.resolve(db, "session", "claude:s1", "branch", 200)
    assert (r.state, r.value, r.evidence_id) == ("unattributed", None, None)
    assert db.execute("SELECT count(*) FROM effective_attributions").fetchone()[0] == 0


def test_usage_event_inherits_from_session_at_query_time(db):
    uid = add_event(db, "claude:s1")
    assert attribution.effective(db, "usage_event", uid, "branch").state == "unattributed"
    attribution.add_evidence(db, ev("main"))
    attribution.resolve(db, "session", "claude:s1", "branch", 500)
    r = attribution.effective(db, "usage_event", uid, "branch")
    assert (r.subject_kind, r.subject_id, r.state, r.value) == ("usage_event", str(uid), "attributed", "main")
    # computed, not stored
    assert db.execute("SELECT count(*) FROM effective_attributions"
                      " WHERE subject_kind = 'usage_event'").fetchone()[0] == 0
    # a change to the session shows through without touching the event
    attribution.add_evidence(db, ev("dev"))
    attribution.resolve(db, "session", "claude:s1", "branch", 500)
    assert attribution.effective(db, "usage_event", uid, "branch").value == "dev"


def test_usage_event_own_evidence_beats_session(db):
    uid = add_event(db, "claude:s1")
    attribution.add_evidence(db, ev("main"))
    attribution.resolve(db, "session", "claude:s1", "branch", 500)
    attribution.add_evidence(db, ev("feature", kind="usage_event", subject=str(uid)))
    attribution.resolve(db, "usage_event", str(uid), "branch", 500)
    assert attribution.effective(db, "usage_event", uid, "branch").value == "feature"


def test_usage_event_without_session(db):
    uid = add_event(db, None)
    r = attribution.effective(db, "usage_event", uid, "branch")
    assert (r.state, r.note) == ("unattributed", "no-session-id")


# --- account at a point in time (A19) ---------------------------------------

def test_account_at_follows_the_login_in_use_at_that_time(db):
    a = accounts.create_account(db, "anthropic")
    b = accounts.create_account(db, "anthropic")
    for value, t in ((a, 100), (b, 200), (a, 300)):
        attribution.add_evidence(db, AttributionEvidence(
            subject_kind="session", subject_id="omp:s", dimension="account", value=value,
            method="credential_id", source="omp.session", confidence="authoritative",
            validity="historical", first_observed_at=t, last_confirmed_at=t, valid_from=t))
    assert attribution.account_at(db, "omp:s", 150).value == a
    assert attribution.account_at(db, "omp:s", 250).value == b
    assert attribution.account_at(db, "omp:s", 999).value == a
    assert attribution.account_at(db, "omp:s", 50).state == "unattributed"

import re

import pytest

from usage_watch.errors import Problem
from usage_watch.runtime import accounts
from usage_watch.store import connect, migrate

KIND = "anthropic.account_org"


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "usage.db")
    migrate(conn)
    yield conn
    conn.close()


def say(conn, alias, source="claude.json", evidence="reported", key=None, confidence="authoritative"):
    return accounts.assert_alias(conn, provider="anthropic", alias_kind=KIND, alias_hash=alias,
                                 asserted_by=source, evidence=evidence, confidence=confidence,
                                 account_key=key)


def alias_rows(conn):
    return conn.execute("SELECT alias_hash, account_key, asserted_by FROM account_aliases"
                        " ORDER BY alias_id").fetchall()


def test_key_is_random_and_independent_of_alias_order(tmp_path):
    keys = []
    for order in (["h1", "h2"], ["h2", "h1"]):
        conn = connect(tmp_path / f"{order[0]}.db")
        migrate(conn)
        got = {alias: say(conn, alias) for alias in order}
        keys.append(got)
        conn.close()
    for got in keys:
        assert all(re.fullmatch(r"[0-9a-f]{32}", k) for k in got.values())
        assert got["h1"] != got["h2"]
    assert keys[0]["h1"] != keys[1]["h1"]  # never derived from the alias


def test_reconfirm_updates_own_row(db):
    k = say(db, "h1")
    db.execute("UPDATE account_aliases SET last_confirmed = 1")
    assert say(db, "h1") == k
    assert db.execute("SELECT count(*), max(last_confirmed) FROM account_aliases").fetchone()[1] > 1
    assert len(alias_rows(db)) == 1


def test_same_alias_from_second_source_is_a_separate_row(db):
    k = say(db, "h1", source="claude.json")
    assert say(db, "h1", source="claude.otel") == k
    assert alias_rows(db) == [("h1", k, "claude.json"), ("h1", k, "claude.otel")]
    assert db.execute("SELECT count(*) FROM accounts").fetchone()[0] == 1


def test_conflicting_assertion_is_detected(db):
    a = say(db, "h1", source="claude.json")
    b = accounts.create_account(db, "anthropic")
    say(db, "h1", source="transcript", key=b)
    assert len(alias_rows(db)) == 2  # nothing overwritten
    assert accounts.alias_conflicts(db) == [(KIND, "h1", sorted([a, b]))]
    with pytest.raises(Problem):
        say(db, "h1", source="claude.otel")  # a new source can't pick a side
    # a merge resolves the conflict
    lo, hi = sorted([a, b])
    accounts.merge(db, hi, lo, evidence="user", verified_by=None)
    assert accounts.alias_conflicts(db) == []


def test_co_reported_rows_are_not_identification(db):
    a = say(db, "h1")
    b = say(db, "h2")
    accounts.assert_alias(db, provider="anthropic", alias_kind=KIND, alias_hash="h2",
                          asserted_by="omp.report", evidence="co_reported",
                          confidence="observed", account_key=a)
    assert accounts.alias_conflicts(db) == []
    assert say(db, "h2", source="other") == b


def test_merge_canonical_unmerge(db):
    a = accounts.create_account(db, "anthropic")
    b = accounts.create_account(db, "anthropic")
    c = accounts.create_account(db, "anthropic")
    m1 = accounts.merge(db, a, b, evidence="user", verified_by=None)
    m2 = accounts.merge(db, b, c, evidence="user", verified_by=None)
    assert [accounts.canonical(db, k) for k in (a, b, c)] == [c, c, c]
    accounts.unmerge(db, m2)
    assert [accounts.canonical(db, k) for k in (a, b, c)] == [b, b, c]
    accounts.unmerge(db, m1)
    assert accounts.canonical(db, a) == a
    assert db.execute("SELECT count(*) FROM account_merges WHERE revoked_at IS NOT NULL").fetchone()[0] == 2


def test_unmerge_moves_no_alias(db):
    a = say(db, "h1")
    b = say(db, "h2")
    before = alias_rows(db)
    m = accounts.merge(db, a, b, evidence="user", verified_by=None)
    assert say(db, "h1") == b
    accounts.unmerge(db, m)
    assert alias_rows(db) == before
    assert say(db, "h1") == a


def test_cycle_is_refused(db):
    a = accounts.create_account(db, "anthropic")
    b = accounts.create_account(db, "anthropic")
    accounts.merge(db, a, b, evidence="user", verified_by=None)
    with pytest.raises(Problem, match="cycle"):
        accounts.merge(db, b, a, evidence="user", verified_by=None)


def test_second_live_outgoing_merge_is_refused(db):
    a, b, c = (accounts.create_account(db, "anthropic") for _ in range(3))
    m = accounts.merge(db, a, b, evidence="user", verified_by=None)
    with pytest.raises(Problem) as err:
        accounts.merge(db, a, c, evidence="user", verified_by=None)
    assert "already has a live merge" in err.value.what and err.value.fix and err.value.expected
    accounts.unmerge(db, m)
    accounts.merge(db, a, c, evidence="user", verified_by=None)  # allowed once revoked


def test_evidence_merge_goes_into_smaller_key_and_is_not_recreated(db):
    a = accounts.create_account(db, "anthropic")
    b = accounts.create_account(db, "anthropic")
    lo, hi = sorted([a, b])
    with pytest.raises(Problem, match="larger key"):
        accounts.merge(db, lo, hi, evidence="co_reported", verified_by="doc:x")
    m = accounts.merge(db, hi, lo, evidence="co_reported", verified_by="doc:x")
    accounts.unmerge(db, m)
    with pytest.raises(Problem, match="revoked"):
        accounts.merge(db, hi, lo, evidence="co_reported", verified_by="doc:x")
    accounts.merge(db, hi, lo, evidence="user", verified_by=None)

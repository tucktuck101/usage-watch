import sqlite3

import pytest

from usage_watch import store
from usage_watch.errors import Problem
from usage_watch.store import SCHEMA_VERSION, TABLE_OWNERS, connect, migrate, schema_status

D3_TABLES = {
    "usage_observations", "usage_events", "event_observations", "attribution_evidence",
    "effective_attributions", "capacity_samples", "limit_events", "cost_events",
    "state_samples", "context_events", "sessions", "checkouts", "accounts",
    "account_aliases", "account_merges", "watermarks", "collector_status", "runtime",
    "looks", "nudges", "schema_version",
}

ACCT_A = "0" * 31 + "a"
ACCT_B = "0" * 31 + "b"


def fake_hash(s: str) -> str:
    """Stand-in for the keyed hash (identity.py is built elsewhere)."""
    return "h:" + s


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "usage.db")
    migrate(conn)
    yield conn
    conn.close()


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def refused(conn, sql, params=()):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, params)


def add_obs(conn, key, source="claude.transcript", stream="claude:s1"):
    return conn.execute(
        "INSERT INTO usage_observations (source, stream_key, source_request_key, confidence, observed_at)"
        " VALUES (?, ?, ?, 'authoritative', 1)", (source, stream, key)).lastrowid


def add_event(conn, obs_id):
    return conn.execute(
        "INSERT INTO usage_events (accounting_observation_id, observed_at, reconciled_version)"
        " VALUES (?, 1, 1)", (obs_id,)).lastrowid


# --- connection and migration ------------------------------------------------

def test_connect_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert store.default_path() == tmp_path / "usage-watch" / "usage.db"
    conn = connect()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()
    assert (tmp_path / "usage-watch" / "usage.db").exists()


def test_default_path_without_xdg(monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", "/home/x")
    assert str(store.default_path()) == "/home/x/.local/state/usage-watch/usage.db"


def test_migrate_empty_database_creates_every_table(tmp_path):
    conn = connect(tmp_path / "usage.db")
    assert migrate(conn) == SCHEMA_VERSION == 1
    assert D3_TABLES <= tables(conn)
    assert conn.execute("SELECT version FROM schema_version").fetchall() == [(1,)]
    assert not list(tmp_path.glob("*.bak")), "an empty database needs no backup"


def test_second_migrate_is_a_no_op(db, tmp_path):
    before = db.execute("SELECT version, migrated_at FROM schema_version").fetchall()
    assert migrate(db) == 1
    assert db.execute("SELECT version, migrated_at FROM schema_version").fetchall() == before
    assert not list(tmp_path.glob("*.bak"))


def test_migrating_a_non_empty_database_takes_a_backup(tmp_path):
    path = tmp_path / "usage.db"
    conn = connect(path)
    conn.execute("CREATE TABLE legacy (x)")
    conn.execute("INSERT INTO legacy VALUES (42)")
    migrate(conn)
    conn.close()
    [bak] = tmp_path.glob("usage.db.v0.*.bak")
    copy = sqlite3.connect(bak)
    assert copy.execute("SELECT x FROM legacy").fetchall() == [(42,)]
    assert "schema_version" not in tables(copy)
    copy.close()


def test_migrate_refuses_a_newer_schema(db):
    db.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION + 1,))
    with pytest.raises(Problem, match="newer"):
        migrate(db)


def test_every_table_has_an_owner():
    assert set(TABLE_OWNERS) == D3_TABLES
    assert TABLE_OWNERS["nudges"] == "nudge_policy"
    assert TABLE_OWNERS["looks"] == "views"
    assert {TABLE_OWNERS[t] for t in D3_TABLES - {"nudges", "looks"}} == {"collector_runtime"}


def test_ownership_covers_exactly_the_migrated_tables(db):
    assert tables(db) == set(TABLE_OWNERS)


# --- read-only and schema status ----------------------------------------------

def test_readonly_connection_cannot_write(db, tmp_path):
    ro = connect(tmp_path / "usage.db", readonly=True)
    assert ro.execute("SELECT count(*) FROM sessions").fetchone() == (0,)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("INSERT INTO sessions (session_key, harness, session_id) VALUES ('c:1', 'c', '1')")
    ro.close()


def test_readonly_connect_to_missing_store_is_a_problem(tmp_path):
    with pytest.raises(Problem):
        connect(tmp_path / "absent.db", readonly=True)
    assert not (tmp_path / "absent.db").exists()


def test_schema_status_equal(db):
    s = schema_status(db)
    assert (s.state, s.found, s.expected, s.message, s.readable) == ("equal", 1, 1, None, True)


def test_schema_status_older(tmp_path):
    conn = connect(tmp_path / "usage.db")
    s = schema_status(conn)
    assert s.state == "older" and not s.readable
    assert "the collector hasn't migrated yet" in s.message
    s = schema_status(conn, expected=1)
    assert s.found == 0


def test_schema_status_older_than_newer_code(db):
    s = schema_status(db, expected=SCHEMA_VERSION + 1)
    assert s.state == "older"
    assert "the collector hasn't migrated yet" in s.message


def test_schema_status_newer(db):
    db.execute("UPDATE schema_version SET version = 7")
    s = schema_status(db)
    assert s.state == "newer" and s.found == 7
    assert "upgrade usage-watch" in s.message


# --- identities refuse duplicates ----------------------------------------------

def test_usage_observation_identity(db):
    add_obs(db, "r1")
    with pytest.raises(sqlite3.IntegrityError):
        add_obs(db, "r1")
    add_obs(db, "r1", stream="claude:s2")  # scoped to its stream
    add_obs(db, "r1", source="otlp.receiver")


@pytest.mark.parametrize("col", ["source", "stream_key", "source_request_key"])
def test_usage_observation_identity_columns_not_null(db, col):
    vals = {"source": "s", "stream_key": "k", "source_request_key": "r"}
    vals[col] = None
    refused(db, "INSERT INTO usage_observations (source, stream_key, source_request_key, confidence,"
                " observed_at) VALUES (?, ?, ?, 'observed', 1)", tuple(vals.values()))


def test_provider_request_key_is_not_unique(db):
    for key in ("a", "b"):
        db.execute("INSERT INTO usage_observations (source, stream_key, source_request_key, confidence,"
                   " observed_at, provider_request_key) VALUES ('s', 'k', ?, 'observed', 1, ?)",
                   (key, fake_hash("req")))


def test_one_accounting_observation_per_event(db):
    obs = add_obs(db, "r1")
    add_event(db, obs)
    with pytest.raises(sqlite3.IntegrityError):
        add_event(db, obs)
    refused(db, "INSERT INTO usage_events (accounting_observation_id, observed_at, reconciled_version)"
                " VALUES (NULL, 1, 1)")


def test_accounting_observation_must_exist(db):
    refused(db, "INSERT INTO usage_events (accounting_observation_id, observed_at, reconciled_version)"
                " VALUES (999, 1, 1)")


def test_secondary_observation_supports_at_most_one_event(db):
    e1, e2 = add_event(db, add_obs(db, "p1")), add_event(db, add_obs(db, "p2"))
    otel = add_obs(db, "x", source="otlp.receiver")
    db.execute("INSERT INTO event_observations VALUES (?, ?, 'supporting', '')", (e1, otel))
    refused(db, "INSERT INTO event_observations VALUES (?, ?, 'supporting', '')", (e2, otel))
    refused(db, "INSERT INTO event_observations VALUES (?, ?, 'supporting', '')", (e1, otel))


def test_one_metadata_source_per_event_and_field(db):
    e1 = add_event(db, add_obs(db, "p1"))
    a, b = add_obs(db, "x", source="otlp.receiver"), add_obs(db, "y", source="otlp.receiver")
    db.execute("INSERT INTO event_observations VALUES (?, ?, 'metadata', 'model')", (e1, a))
    refused(db, "INSERT INTO event_observations VALUES (?, ?, 'metadata', 'model')", (e1, b))
    db.execute("INSERT INTO event_observations VALUES (?, ?, 'metadata', 'provider')", (e1, b))


def test_event_observations_rejects_accounting_role(db):
    e1 = add_event(db, add_obs(db, "p1"))
    other = add_obs(db, "x", source="otlp.receiver")
    refused(db, "INSERT INTO event_observations VALUES (?, ?, 'accounting', '')", (e1, other))


def test_event_observations_field_sentinels(db):
    e1 = add_event(db, add_obs(db, "p1"))
    other = add_obs(db, "x", source="otlp.receiver")
    refused(db, "INSERT INTO event_observations VALUES (?, ?, 'supporting', NULL)", (e1, other))
    refused(db, "INSERT INTO event_observations VALUES (?, ?, 'supporting', 'model')", (e1, other))
    refused(db, "INSERT INTO event_observations VALUES (?, ?, 'metadata', '')", (e1, other))


EVIDENCE = ("INSERT INTO attribution_evidence (subject_kind, subject_id, dimension, value, method,"
            " source, confidence, validity, valid_from, first_observed_at, last_confirmed_at)"
            " VALUES ('session', 'claude:s1', 'branch', 'main', 'record', 'claude.transcript',"
            " 'authoritative', 'historical', ?, 1, 1)")


def test_historical_evidence_with_unbounded_start_is_a_duplicate(db):
    db.execute(EVIDENCE, (0,))
    refused(db, EVIDENCE, (0,))
    refused(db, EVIDENCE, (None,))
    db.execute(EVIDENCE, (500,))  # a different valid_from is a different row


def test_evidence_valid_from_defaults_to_unbounded(db):
    db.execute("INSERT INTO attribution_evidence (subject_kind, subject_id, dimension, value, method,"
               " source, confidence, validity, first_observed_at, last_confirmed_at)"
               " VALUES ('usage_event', 5, 'account', ?, 'otel', 's', 'authoritative', 'historical', 1, 1)",
               (fake_hash("acct"),))
    assert db.execute("SELECT valid_from, typeof(subject_id) FROM attribution_evidence").fetchone() == (0, "text")
    # an integer subject id and its decimal text are one subject
    refused(db, "INSERT INTO attribution_evidence (subject_kind, subject_id, dimension, value, method,"
                " source, confidence, validity, first_observed_at, last_confirmed_at)"
                " VALUES ('usage_event', '5', 'account', ?, 'otel', 's', 'authoritative', 'historical', 1, 1)",
                (fake_hash("acct"),))


def test_evidence_enumerations_checked(db):
    refused(db, EVIDENCE.replace("'historical'", "'forever'"), (0,))
    refused(db, EVIDENCE.replace("'authoritative'", "'certain'"), (0,))
    refused(db, EVIDENCE.replace("'branch'", "'mood'"), (0,))
    refused(db, EVIDENCE.replace("'session'", "'pane'"), (0,))


def test_effective_attribution_one_row_per_subject_dimension(db):
    sql = ("INSERT INTO effective_attributions (subject_kind, subject_id, dimension, state, note)"
           " VALUES ('session', 'claude:s1', 'account', 'unattributed', 'no-session-id')")
    db.execute(sql)
    refused(db, sql)


def test_effective_attribution_value_only_when_attributed(db):
    base = ("INSERT INTO effective_attributions (subject_kind, subject_id, dimension, state, value)"
            " VALUES ('session', 'claude:s1', 'branch', ?, ?)")
    refused(db, base, ("unattributed", "main"))
    refused(db, base, ("attributed", None))
    db.execute(base, ("attributed", "main"))


CAPACITY = ("INSERT INTO capacity_samples (source, stream_key, \"window\", status, confidence,"
            " observed_at, used_pct) VALUES ('omp.usage_cache', ?, ?, 'ok', 'observed', 100, ?)")


def test_capacity_sample_identity(db):
    db.execute(CAPACITY, (fake_hash("login"), "other:unstated", 10.0))
    refused(db, CAPACITY, (fake_hash("login"), "other:unstated", 20.0))
    refused(db, CAPACITY, (fake_hash("login"), None, 20.0))
    db.execute(CAPACITY, (fake_hash("login"), "weekly:opus", 20.0))


def test_capacity_sample_window_and_pct_checked(db):
    refused(db, CAPACITY, ("k", "daily", 1.0))
    refused(db, CAPACITY, ("k", "session", 101.0))


LIMIT = ("INSERT INTO limit_events (source, stream_key, source_key, \"window\", kind, confidence,"
         " observed_at) VALUES ('claude.transcript', 'k', ?, ?, ?, 'authoritative', 1)")


def test_limit_event_identity_excludes_window(db):
    db.execute(LIMIT, ("n1", None, "hit"))
    refused(db, LIMIT, ("n1", "session", "hit"))
    refused(db, LIMIT, (None, None, "hit"))
    refused(db, LIMIT, ("n2", None, "maybe"))


COST = ("INSERT INTO cost_events (scope_kind, scope_id, source, basis, price_version,"
        " cost_usd_micros, confidence, observed_at) VALUES ('request', '1', 'omp.session', ?, ?, ?,"
        " 'authoritative', 1)")


def test_cost_event_identity_with_empty_price_version(db):
    db.execute(COST, ("harness_estimate", "", 100))
    refused(db, COST, ("harness_estimate", "", 200))
    refused(db, COST, ("harness_estimate", None, 200))
    db.execute(COST, ("actual_billed", "", 200))


def test_cost_event_price_version_rules(db):
    refused(db, COST, ("list_price", "", 1))           # list_price names its table
    refused(db, COST, ("actual_billed", "2026-09", 1))  # other bases use ''
    db.execute(COST, ("list_price", "2026-09:opus", 1))
    db.execute(COST, ("list_price", "2026-10:opus", 2))  # revisions sit alongside
    refused(db, COST, ("guess", "", 1))


def test_state_sample_identity(db):
    sql = ("INSERT INTO state_samples (pane, observed_at, state, source) VALUES ('%1', 1, ?, 'screen')")
    db.execute(sql, ("busy",))
    refused(db, sql, ("idle",))
    refused(db, "INSERT INTO state_samples (pane, observed_at, state, source, confidence)"
                " VALUES ('%1', 2, 'busy', 'screen', 'observed')")
    refused(db, "INSERT INTO state_samples (pane, observed_at, state, source) VALUES (NULL, 3, 'busy', 's')")


def test_session_identity_and_key_form(db):
    sql = "INSERT INTO sessions (session_key, harness, session_id) VALUES (?, ?, ?)"
    db.execute(sql, ("claude:s1", "claude", "s1"))
    refused(db, sql, ("claude:s1", "claude", "s1"))
    refused(db, sql, ("claude:s2", "codex", "s2"))


def test_checkout_identity(db):
    sql = "INSERT INTO checkouts (checkout_id) VALUES (?)"
    db.execute(sql, (fake_hash("/repo/.git"),))
    refused(db, sql, (fake_hash("/repo/.git"),))


def test_account_key_is_128_bit_hex(db):
    sql = "INSERT INTO accounts (account_key, provider, first_seen, last_seen) VALUES (?, 'anthropic', 1, 1)"
    db.execute(sql, (ACCT_A,))
    refused(db, sql, (ACCT_A,))
    refused(db, sql, ("not-hex",))
    refused(db, sql, ("A" * 32,))


ALIAS = ("INSERT INTO account_aliases (alias_kind, alias_hash, account_key, asserted_by, evidence,"
         " confidence, first_seen, last_confirmed) VALUES ('anthropic.account_org', ?, ?, ?, 'reported',"
         " 'authoritative', 1, 1)")


def test_account_alias_identity(db):
    for key in (ACCT_A, ACCT_B):
        db.execute("INSERT INTO accounts (account_key, provider, first_seen, last_seen)"
                   " VALUES (?, 'anthropic', 1, 1)", (key,))
    db.execute(ALIAS, (fake_hash("u+o"), ACCT_A, "claude.otel"))
    refused(db, ALIAS, (fake_hash("u+o"), ACCT_A, "claude.otel"))
    db.execute(ALIAS, (fake_hash("u+o"), ACCT_A, "claude.home"))  # another source: own row
    db.execute(ALIAS, (fake_hash("u+o"), ACCT_B, "claude.otel"))  # a conflict: own row
    refused(db, ALIAS, (fake_hash("u+o"), None, "x"))


def test_account_merges(db):
    for key in (ACCT_A, ACCT_B):
        db.execute("INSERT INTO accounts (account_key, provider, first_seen, last_seen)"
                   " VALUES (?, 'anthropic', 1, 1)", (key,))
    sql = ("INSERT INTO account_merges (from_key, into_key, evidence, created_at, revoked_at)"
           " VALUES (?, ?, 'user', 1, ?)")
    db.execute(sql, (ACCT_B, ACCT_A, 5))           # revoked
    db.execute(sql, (ACCT_B, ACCT_A, None))
    refused(db, sql, (ACCT_B, ACCT_A, None))       # one unrevoked outgoing merge
    refused(db, sql, (ACCT_A, ACCT_A, None))


@pytest.mark.parametrize("table, cols", [
    ("watermarks", "(collector, file, path) VALUES ('c', 'f', 'p')"),
    ("collector_status", "(collector) VALUES ('c')"),
    ("looks", "(who, \"view\") VALUES ('cli', 'status')"),
])
def test_runtime_table_identities(db, table, cols):
    db.execute(f"INSERT INTO {table} {cols}")
    refused(db, f"INSERT INTO {table} {cols}")


def test_schema_version_has_one_row(db):
    refused(db, "INSERT INTO schema_version (id, version, migrated_at) VALUES (2, 1, 1)")

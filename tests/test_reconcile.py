import json
import sqlite3
from dataclasses import replace

import pytest

from usage_watch import model
from usage_watch.runtime import reconcile as rc
from usage_watch.store import connect, migrate

PRK = "prk0000000000001"


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "usage.db")
    migrate(conn)
    yield conn
    conn.close()


def transcript(key="msg1+req1", prk=PRK, output=100, **kw):
    fields = dict(uncached_input_tokens=10, cache_read_input_tokens=1000,
                  cache_write_input_tokens=50, output_tokens=output)
    fields.update(kw)
    return model.UsageObservation(
        source="claude.transcript", stream_key="claude:s1", source_request_key=key,
        provider_request_key=prk, confidence="authoritative", observed_at=1000,
        harness="claude", provider="anthropic", model="claude-x", session_key="claude:s1",
        **fields)


def otel(key="req1", prk=PRK, output=100, **kw):
    fields = dict(uncached_input_tokens=10, cache_read_input_tokens=1000,
                  cache_write_input_tokens=50, output_tokens=output)
    fields.update(kw)
    return model.UsageObservation(
        source="otlp.receiver", stream_key="claude:s1", source_request_key=key,
        provider_request_key=prk, confidence="authoritative", observed_at=1002,
        harness="claude", provider="anthropic", session_key="claude:s1", **fields)


def largest_output(old, new):
    """Claude's counting rule: keep the snapshot with the largest output_tokens."""
    return new if (new.output_tokens or 0) >= (old.output_tokens or 0) else old


def primary(conn, obs, merge=None):
    oid = rc.upsert_observation(conn, obs, merge)
    return oid, rc.reconcile_observation(conn, oid, primary=True)


def secondary(conn, obs):
    oid = rc.upsert_observation(conn, obs)
    return oid, rc.reconcile_observation(conn, oid, primary=False)


def events(conn):
    return conn.execute("SELECT usage_id, accounting_observation_id, output_tokens"
                        " FROM usage_events ORDER BY usage_id").fetchall()


def totals(conn):
    cols = ", ".join(f"SUM({f})" for f in rc.TOKEN_FIELDS)
    return conn.execute(f"SELECT count(*), {cols} FROM usage_events").fetchone()


def test_primary_observation_creates_exactly_one_event(db):
    oid, usage_id = primary(db, transcript())
    assert events(db) == [(usage_id, oid, 100)]
    row = db.execute("SELECT observed_at, session_key, reconciled_version, disagreement, auxiliary"
                     " FROM usage_events").fetchone()
    assert row == (1000, "claude:s1", rc.CURRENT_RECONCILE_VERSION, None, 0)
    assert rc.link_state(db, oid) == "primary"
    # Reconciling the same observation again adds nothing.
    assert rc.reconcile_observation(db, oid, primary=True) == usage_id
    assert len(events(db)) == 1


def test_streaming_snapshot_updates_observation_and_event_in_place(db):
    oid, usage_id = primary(db, transcript(output=5), largest_output)
    oid2, usage_id2 = primary(db, replace(transcript(output=240), observed_at=1500), largest_output)
    assert (oid2, usage_id2) == (oid, usage_id)
    assert db.execute("SELECT count(*), max(output_tokens) FROM usage_observations").fetchone() == (1, 240)
    assert events(db) == [(usage_id, oid, 240)]
    # observed_at is copied at creation and immutable (D1).
    assert db.execute("SELECT observed_at FROM usage_events").fetchone() == (1000,)
    # An earlier, smaller snapshot arriving late doesn't regress the count.
    primary(db, transcript(output=7), largest_output)
    assert events(db) == [(usage_id, oid, 240)]


def test_upsert_without_merge_replaces(db):
    oid = rc.upsert_observation(db, transcript(output=240))
    assert rc.upsert_observation(db, transcript(output=5)) == oid
    assert db.execute("SELECT output_tokens FROM usage_observations").fetchone() == (5,)


def test_secondary_before_primary_is_orphan_then_links(db):
    sid, linked_to = secondary(db, otel())
    assert linked_to is None
    assert rc.link_state(db, sid) == "orphan"
    assert rc.orphan_counts(db) == {"otlp.receiver": 1}

    _, usage_id = primary(db, transcript())
    assert rc.link_state(db, sid) == "linked"
    assert rc.orphan_counts(db) == {}
    assert db.execute("SELECT usage_id, role, field FROM event_observations"
                      " WHERE observation_id = ?", (sid,)).fetchall() == [(usage_id, "supporting", "")]


def test_secondary_after_primary_links_immediately(db):
    _, usage_id = primary(db, transcript())
    sid, linked_to = secondary(db, otel())
    assert linked_to == usage_id
    assert rc.link_state(db, sid) == "linked"
    # Reconciling it again keeps one link: it supports at most one event.
    assert rc.reconcile_observation(db, sid, primary=False) == usage_id
    assert db.execute("SELECT count(*) FROM event_observations").fetchone() == (1,)


def test_secondary_without_matching_key_stays_orphan(db):
    primary(db, transcript())
    s_other, r1 = secondary(db, otel(key="req2", prk="prk-other"))
    s_null, r2 = secondary(db, otel(key="req3", prk=None))
    assert r1 is None and r2 is None
    assert rc.link_state(db, s_other) == rc.link_state(db, s_null) == "orphan"


def test_unlinked_secondary_never_affects_totals(db):
    primary(db, transcript())
    before = totals(db)
    secondary(db, otel(key="req9", prk="prk-unmatched", output=99999))
    secondary(db, otel(key="req10", prk=None, output=5))
    assert totals(db) == before
    # A linked one doesn't count either: only the accounting observation does.
    secondary(db, otel(output=12345))
    assert totals(db) == before


def test_disagreement_is_recorded(db):
    _, usage_id = primary(db, transcript(output=12400))
    secondary(db, otel(output=12402))
    secondary(db, replace(otel(key="req1-dup", output=12390), source="other.secondary"))
    got = json.loads(db.execute("SELECT disagreement FROM usage_events WHERE usage_id = ?",
                                (usage_id,)).fetchone()[0])
    assert got == {"output_tokens": 10}


def test_agreement_leaves_disagreement_null_and_null_fields_are_skipped(db):
    primary(db, transcript(cache_read_input_tokens=None))
    secondary(db, otel(uncached_input_tokens=None))  # other fields equal
    assert db.execute("SELECT disagreement FROM usage_events").fetchone() == (None,)


def test_disagreement_follows_a_primary_update(db):
    primary(db, transcript(output=5), largest_output)
    secondary(db, otel(output=240))
    assert json.loads(db.execute("SELECT disagreement FROM usage_events").fetchone()[0]) == {
        "output_tokens": 235}
    primary(db, transcript(output=240), largest_output)
    assert db.execute("SELECT disagreement FROM usage_events").fetchone() == (None,)


def test_second_event_cannot_take_the_same_accounting_observation(db):
    oid, _ = primary(db, transcript())
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO usage_events (accounting_observation_id, observed_at,"
                   " reconciled_version) VALUES (?, 1, 1)", (oid,))
    rc.reconcile_observation(db, oid, primary=True)
    assert len(events(db)) == 1


def test_reprocess_outdated_keeps_ids(db):
    oid, usage_id = primary(db, transcript(output=240))
    sid, _ = secondary(db, otel(output=240))
    # Simulate an older rule version, with stale values the rule would fix.
    db.execute("UPDATE usage_events SET reconciled_version = 0, output_tokens = 1,"
               " disagreement = 'stale'")
    assert rc.reprocess_outdated(db) == 1
    assert events(db) == [(usage_id, oid, 240)]
    assert db.execute("SELECT reconciled_version, disagreement FROM usage_events").fetchone() == (
        rc.CURRENT_RECONCILE_VERSION, None)
    assert rc.link_state(db, sid) == "linked"
    assert rc.reprocess_outdated(db) == 0


def test_orphan_counts_per_source(db):
    primary(db, transcript())
    secondary(db, otel(key="a", prk="x1"))
    secondary(db, otel(key="b", prk=None))
    secondary(db, replace(otel(key="c", prk="x2"), source="codex.otel"))
    secondary(db, otel(key="d"))  # links
    assert rc.orphan_counts(db) == {"codex.otel": 1, "otlp.receiver": 2}


def test_null_tokens_stay_null(db):
    _, usage_id = primary(db, transcript(uncached_input_tokens=None, reasoning_output_tokens=None))
    row = db.execute("SELECT uncached_input_tokens, reasoning_output_tokens, output_tokens"
                     " FROM usage_events WHERE usage_id = ?", (usage_id,)).fetchone()
    assert row == (None, None, 100)
    rc.reprocess_outdated(db)
    db.execute("UPDATE usage_events SET reconciled_version = 0")
    rc.reprocess_outdated(db)
    assert db.execute("SELECT uncached_input_tokens, reasoning_output_tokens"
                      " FROM usage_events").fetchone() == (None, None)


def test_metadata_link(db):
    _, usage_id = primary(db, transcript())
    sid, _ = secondary(db, otel())
    rc.add_metadata_link(db, usage_id, sid, "model")
    rc.add_metadata_link(db, usage_id, sid, "model")  # idempotent
    assert db.execute("SELECT role, field FROM event_observations WHERE role = 'metadata'"
                      ).fetchall() == [("metadata", "model")]
    with pytest.raises(ValueError):
        rc.add_metadata_link(db, usage_id, sid, "output_tokens")
    orphan, _ = secondary(db, otel(key="z", prk=None))
    with pytest.raises(ValueError):
        rc.add_metadata_link(db, usage_id, orphan, "provider")


def test_works_inside_the_callers_transaction(db):
    db.execute("BEGIN IMMEDIATE")
    primary(db, transcript())
    secondary(db, otel())
    assert db.in_transaction
    db.execute("ROLLBACK")
    assert events(db) == []

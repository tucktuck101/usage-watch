"""Capacity anchors for display, over a migrated store."""

import pytest

from usage_watch.capacity import DISPLAY_AGE_MS, Anchor, account_anchors
from usage_watch.store import connect, migrate

NOW = 1_800_000_000_000
MIN, HOUR = 60_000, 3_600_000
ACCT = "a" * 32
OTHER = "b" * 32


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "usage.db")
    migrate(conn)
    yield conn
    conn.close()


def anchor(conn, window, used, *, source="omp.usage_cache", stream="st", at=NOW - MIN,
           resets=NOW + HOUR, status="ok", acct=ACCT, state="attributed"):
    conn.execute("INSERT INTO capacity_samples (source, stream_key, \"window\", used_pct,"
                 " resets_at, status, confidence, observed_at) VALUES (?, ?, ?, ?, ?, ?,"
                 " 'observed', ?)", (source, stream, window, used, resets, status, at))
    conn.execute("INSERT INTO effective_attributions (subject_kind, subject_id, dimension, state,"
                 " value, confidence) VALUES ('capacity_sample', ?, 'account', ?, ?, 'observed')",
                 (f"{source}|{stream}|{window}|{at}", state,
                  acct if state == "attributed" else None))


def test_account_anchors_newest_first_attributed_only_and_filtered(db):
    anchor(db, "session", 10, at=NOW - 2 * MIN)
    anchor(db, "session", 20, at=NOW - MIN)
    anchor(db, "weekly", 30, acct=OTHER)
    anchor(db, "weekly", 40, at=NOW - 3 * MIN, state="ambiguous")
    anchor(db, "session", 50, source="omp.usage_history", at=NOW - 4 * MIN)
    anchor(db, "session", 60, at=NOW - 2 * HOUR)  # before `since`
    mine = account_anchors(db, ACCT, NOW - HOUR)
    assert [a.used_pct for a in mine] == [20, 10, 50]
    assert {a.used_pct for a in account_anchors(db, None, NOW - HOUR)} == {10, 20, 30, 50}
    assert [a.used_pct for a in account_anchors(db, ACCT, NOW - HOUR, {"omp.usage_cache"})] \
        == [20, 10]


def test_anchor_methods_use_display_age():
    a = Anchor("omp.usage_cache", "st", "session", 30.0, NOW + HOUR, "ok", "observed",
               NOW - DISPLAY_AGE_MS["omp.usage_cache"], ACCT)
    assert a.remaining() == 70 and a.current(NOW) and a.fresh(NOW)
    assert not a.fresh(NOW + 1) and not a.current(NOW + HOUR)
    assert a.describe(NOW)["age_s"] == DISPLAY_AGE_MS["omp.usage_cache"] // 1000
    unknown = Anchor("estimate", "st", "session", None, None, "ok", "observed", NOW, ACCT)
    assert unknown.remaining() is None and not unknown.current(NOW) and not unknown.fresh(NOW)

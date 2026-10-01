"""A1 pool alerts over a migrated store."""

import pytest

from usage_watch.alerts import Alerts
from usage_watch.store import connect, migrate

NOW_S = 1_800_000_000
NOW = NOW_S * 1000
MIN, HOUR = 60_000, 3_600_000
ACCT = "a" * 32


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "usage.db")
    migrate(c)
    c.execute("INSERT INTO accounts (account_key, provider, label, first_seen, last_seen)"
              " VALUES (?, 'anthropic', 'team', 0, 0)", (ACCT,))
    yield c
    c.close()


def anchor(conn, used, *, at, window="session", resets=NOW + HOUR, source="omp.usage_cache",
           acct=ACCT):
    conn.execute("INSERT INTO capacity_samples (source, stream_key, \"window\", used_pct,"
                 " resets_at, status, confidence, observed_at) VALUES (?, 's', ?, ?, ?, 'ok',"
                 " 'observed', ?)", (source, window, used, resets, at))
    if acct:
        conn.execute("INSERT INTO effective_attributions (subject_kind, subject_id, dimension,"
                     " state, value, confidence) VALUES ('capacity_sample', ?, 'account',"
                     " 'attributed', ?, 'observed')", (f"{source}|s|{window}|{at}", acct))


def make(conn, clock):
    notes = []
    return Alerts(conn, clock=lambda: clock[0], notify=lambda t, m: notes.append(m)), notes


def test_fires_once_per_threshold_crossing(conn):
    clock = [NOW_S]
    a, notes = make(conn, clock)
    anchor(conn, 70, at=NOW - MIN)
    assert a.check() == []
    anchor(conn, 85, at=NOW)                       # 15% left: crosses 20
    msgs = a.check()
    assert len(msgs) == 1 and "team: session" in msgs[0] and "at or below 20%" in msgs[0]
    assert a.check() == []                         # same crossing: once
    anchor(conn, 88, at=NOW + MIN)
    clock[0] += 60
    assert a.check() == []                         # still in the 20 band
    anchor(conn, 96, at=NOW + 2 * MIN)             # 4% left: crosses 5
    clock[0] += 60
    msgs = a.check()
    assert len(msgs) == 1 and "at or below 5%" in msgs[0]
    assert a.check() == []
    assert len(notes) == 2


def test_new_window_instance_fires_again(conn):
    clock = [NOW_S]
    a, _ = make(conn, clock)
    anchor(conn, 90, at=NOW)
    assert len(a.check()) == 1
    # the window reset; the new instance runs low too
    anchor(conn, 90, at=NOW + 2 * HOUR, resets=NOW + 6 * HOUR)
    clock[0] += 2 * 3600
    assert len(a.check()) == 1


def test_jumping_past_both_thresholds_gives_one_message(conn):
    a, notes = make(conn, [NOW_S])
    anchor(conn, 99, at=NOW)
    msgs = a.check()
    assert len(msgs) == 1 and "at or below 5%" in msgs[0] and len(notes) == 1


def test_only_anchors_within_display_age(conn):
    a, _ = make(conn, [NOW_S])
    anchor(conn, 99, at=NOW - 16 * MIN)                                      # omp: 15 min
    assert a.check() == []
    anchor(conn, 99, at=NOW - 50 * MIN, source="claude.cached_utilization")  # 60 min
    assert len(a.check()) == 1


def test_unattributed_and_ended_anchors_are_ignored(conn):
    a, _ = make(conn, [NOW_S])
    anchor(conn, 99, at=NOW, acct=None)
    anchor(conn, 99, at=NOW, window="weekly", resets=NOW - MIN)
    assert a.check() == []

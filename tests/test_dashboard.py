import datetime as dt

from usage_watch import dashboard

NOW_MS = 1_790_000_000_000


def pool(label, window, remaining, *, stale=False, resets=None, source="claude.statusline", age="1m"):
    return {"account_label": label, "window": window, "remaining_pct": remaining,
            "resets_at": resets, "stale": stale, "source": source, "age": age}


def test_bar_and_until():
    assert dashboard.bar(50, 10) == "█████░░░░░"
    assert dashboard.bar(None, 4) == "    "
    assert dashboard.until(None) == ""
    now = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    assert dashboard.until(now + dt.timedelta(hours=2, minutes=5), now) == "2h05m"
    assert dashboard.until_ms(NOW_MS + 90 * 60_000, NOW_MS) == "1h30m"
    assert dashboard.until_ms(NOW_MS - 1, NOW_MS) == "now"
    assert dashboard.until_ms(None, NOW_MS) == ""


def test_pool_lines_show_each_window_its_age_and_stale_mark():
    lines = dashboard.pool_lines([
        pool("team", "session", 3.0, resets=NOW_MS + 3_600_000),
        pool("team", "weekly", 20.0),
        pool("personal", "weekly", 80.0, stale=True, source="claude.cached_utilization", age="2h00m"),
        pool("unattributed (codex.rollout)", "weekly", None),
    ], NOW_MS)
    text = [t for t, _ in lines]
    assert text[0].startswith("team") and "3% left" in text[0] and "resets in 1h00m" in text[0]
    assert lines[0][1] == 3                       # red when nearly out
    assert not text[1].startswith("team") and lines[1][1] == 2   # one label per account
    assert "STALE" in text[2] and "2h00m ago" in text[2] and lines[2][1] == 5  # stale is dim
    assert "?" in text[3]
    assert dashboard.pool_lines([], NOW_MS) == [("no capacity readings in the store yet", 5)]


def test_agent_lines_show_the_screen_state_and_account():
    hint = NOW_MS + 3_600_000
    agents = [
        {"pane": "%1", "harness": "claude", "state": "stalled", "model": "Opus 5.5",
         "account_label": "team", "account_state": "attributed", "reset_hint": hint},
        {"pane": "%2", "harness": "omp", "state": "busy", "model": None,
         "account_label": None, "account_state": "ambiguous", "reset_hint": None},
        {"pane": "%3", "harness": "codex", "state": "stalled", "model": None,
         "account_label": None, "account_state": None, "reset_hint": None},
    ]
    lines = dashboard.agent_lines(agents)
    assert lines[0][1] == 6  # the header
    assert "nudge" not in lines[0][0].lower() and "ACTION" not in lines[0][0]
    clock = dt.datetime.fromtimestamp(hint / 1000).strftime("%H:%M")
    assert lines[1][0].rstrip().endswith(f"retry after {clock}") and lines[1][1] == 3
    assert "team" in lines[1][0] and "Opus 5.5" in lines[1][0]
    assert "ambiguous" in lines[2][0] and lines[2][0].rstrip().endswith("ambiguous")
    assert "retry after" not in lines[3][0]
    assert dashboard.agent_lines([])[1] == ("no agent panes seen in the last 2 minutes", 5)

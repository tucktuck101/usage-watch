import json

from usage_watch import dashboard, pool
from usage_watch.pool import Pools

from fakes import usage_json


def test_bar_and_until():
    assert dashboard.bar(50, 10) == "█████░░░░░"
    assert dashboard.bar(None, 4) == "    "
    assert dashboard.until(None) == ""


def test_pool_lines_show_every_window_and_errors(monkeypatch):
    data = json.loads(usage_json(team_session=3))
    data["errors"] = [{"providerId": "openrouter", "message": "key invalid"}]
    monkeypatch.setattr(pool, "SOURCES", [lambda force: data])
    lines = dashboard.pool_lines(Pools())
    text = "\n".join(t for t, _ in lines)
    assert "claude@team" in text and "fable" in text and "codex" in text
    team_session = next((t, c) for t, c in lines if t.startswith("claude@team"))
    assert "3% left" in team_session[0] and team_session[1] == 3   # red when nearly out
    assert any("openrouter" in t and "key invalid" in t for t, _ in lines)

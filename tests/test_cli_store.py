"""Store-backed commands (F4, V1, V2): `status`, `usage`, `run`, against a
temporary XDG_STATE_HOME. Nothing here touches the real state directory,
real tmux panes or real harness files."""

import json

import pytest

from usage_watch import cli, collectors, queries, sh, store, watcher
from usage_watch.model import AgentStateSample, UsageObservation
from usage_watch.runtime.liveness import liveness

from test_queries import NOW, NOW_MS, build_store


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A temporary state home, no config, and no tmux: every command shells
    out to a recorder that fails, so nothing can reach a real pane."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("USAGE_WATCH_CONFIG", str(tmp_path / "none.toml"))
    calls = []

    def no_shell(cmd, cwd=None, timeout=15):
        calls.append(list(cmd))
        return sh.Result(1, "", "not in tests")
    monkeypatch.setattr(sh, "run", no_shell)
    monkeypatch.setattr(sh, "which", lambda name: None)
    monkeypatch.setattr(watcher, "notify", lambda title, msg: None)
    monkeypatch.setattr(cli.time, "time", lambda: NOW)
    db = tmp_path / "state" / "usage-watch" / "usage.db"
    return {"db": db, "calls": calls, "tmp": tmp_path}


@pytest.fixture
def built(state):
    build_store(state["db"], state["db"].parent / "run.lock")
    return state


def test_status_without_a_store_says_how_to_start_a_collector(state, capsys):
    assert cli.main(["status"]) == 1
    err = capsys.readouterr().err
    assert "no usage-watch store" in err and "usage-watch run --no-nudge" in err
    assert not state["db"].exists()  # a reader never creates the store


def test_status_json_reads_the_store(built, capsys):
    assert cli.main(["status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["liveness"]["state"] == "none"
    assert "start one with: usage-watch run --no-nudge" in data["liveness"]["message"]
    windows = {(p["account_label"], p["window"]) for p in data["pools"]}
    assert ("unattributed (codex.rollout)", "weekly") in windows
    assert [a["pane"] for a in data["agents"]] == ["%1"]
    assert data["agents"][0]["session_key"] == "claude:s1"


def test_status_text_marks_stale_readings(built, capsys):
    assert cli.main(["status"]) == 0
    text = capsys.readouterr().out
    assert text.splitlines()[0].startswith("no collector running")
    assert "STALE" in text and "POOLS" in text and "AGENTS" in text


def test_usage_json_groups_and_counts_unknowns(built, capsys):
    assert cli.main(["usage", "--json", "--by", "account", "--since", "24h"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["by"] == "account"
    assert {r["state"] for r in data["rows"]} == {"attributed", "ambiguous", "unattributed"}
    assert data["total"]["requests"] == sum(r["requests"] for r in data["rows"]) == 8
    assert data["total"]["unknown_requests"] == 1
    assert data["shares"]["basis"] == "requests"
    # piped --json output records no look unless asked (D3)
    conn = store.connect(built["db"], readonly=True)
    assert conn.execute("SELECT count(*) FROM looks").fetchone()[0] == 0
    conn.close()


def test_usage_text_shows_unknowns_and_shares(built, capsys):
    assert cli.main(["usage", "--by", "project", "--no-auxiliary"]) == 0
    text = capsys.readouterr().out
    assert "proj-a" in text and "(unattributed)" in text and "TOTAL" in text
    assert "1 request(s) had a token field unknown" in text
    assert "attributed" in text and "of requests" in text
    assert "auxiliary calls excluded" in text


def test_usage_rejects_an_unreadable_since(built, capsys):
    assert cli.main(["usage", "--since", "last tuesday"]) == 1
    assert "cannot read --since" in capsys.readouterr().err


def test_usage_since_last_records_and_closes_a_look(built, capsys):
    assert cli.main(["usage", "--since-last", "--json", "--mark"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["last_look"]["since_ms"] is None and "last 24h" in first["note"]
    conn = store.connect(built["db"], readonly=True)
    row = conn.execute('SELECT opened_at, last_seen_at, closed_at FROM looks'
                       ' WHERE who = ? AND "view" = ?', ("cli", "usage")).fetchone()
    assert row == (NOW_MS, NOW_MS, NOW_MS)  # a one-shot look opens, renders and closes at once
    conn.close()
    assert cli.main(["usage", "--since-last", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["last_look"]["basis"] == "closed_at" and second["since_ms"] == NOW_MS
    assert second["total"]["requests"] == 0


class Fake:
    """A scripted pull source standing in for the real collectors."""

    def __init__(self):
        self.name, self.primary, self.merge, self.interval_s = "claude.transcript", True, None, 7
        self.calls = 0

    def collect(self, watermark):
        self.calls += 1
        return [UsageObservation(
            source=self.name, stream_key="claude:x", source_request_key=f"r{self.calls}",
            confidence="authoritative", observed_at=NOW_MS, harness="claude", provider="anthropic",
            session_key="claude:x", uncached_input_tokens=1, cache_read_input_tokens=2,
            cache_write_input_tokens=3, output_tokens=4),
            AgentStateSample(pane="%9", observed_at=NOW_MS, state="stalled", source="screen")], None


def test_run_once_no_nudge_collects_into_the_temporary_store(state, monkeypatch, capsys):
    fake = Fake()
    monkeypatch.setattr(collectors, "default_sources", lambda: [fake])
    assert cli.main(["run", "--once", "--no-nudge"]) == 0
    assert fake.calls == 1
    assert state["db"].exists()
    conn = store.connect(state["db"], readonly=True)
    assert conn.execute("SELECT count(*) FROM usage_events").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM runtime").fetchone()[0] == 1
    conn.close()
    assert not any("send-keys" in c for c in state["calls"])   # never typed
    out = capsys.readouterr().out
    assert "collecting;" in out and "every 7s" in out
    assert (state["tmp"] / "state" / "usage-watch" / "usage-watch.log").exists()
    assert liveness(db_path=state["db"]).state == "none"     # the lock was released


def test_run_stops_cleanly_on_ctrl_c(state, monkeypatch, capsys):
    fake = Fake()
    monkeypatch.setattr(collectors, "default_sources", lambda: [fake])

    def interrupt(seconds):
        assert seconds == 2
        raise KeyboardInterrupt
    monkeypatch.setattr(cli.time, "sleep", interrupt)
    assert cli.main(["run", "--no-nudge", "--interval", "2"]) == 0
    assert "stopped" in capsys.readouterr().out
    assert liveness(db_path=state["db"]).state == "none"


def test_doctor_shows_liveness_and_each_collector(built, capsys):
    conn = store.connect(built["db"])
    conn.execute("UPDATE collector_status SET last_error = 'IntegrityError at items[3]:Session.cwd'"
                 " WHERE collector = 'facts'")
    conn.close()
    cli.main(["doctor"])
    text = capsys.readouterr().out
    assert "collector: no collector running" in text
    assert "collector claude.transcript: last ran" in text
    assert "error: IntegrityError at items[3]:Session.cwd" in text
    assert "collector claude.otel" in text and "1 unlinked observation" in text


def test_nudge_without_tmux_types_nothing(state, capsys):
    assert cli.main(["nudge", "%7", "--force"]) == 1
    assert "tmux list-panes failed" in capsys.readouterr().err
    assert not any("send-keys" in c for c in state["calls"])


def test_since_last_marker_is_per_view(built):
    rw = store.connect(built["db"])
    queries.open_look(rw, "dashboard", "dashboard", 10)
    rw.close()
    conn = store.connect(built["db"], readonly=True)
    assert queries.since_last_look(conn, "cli", "usage").since_ms is None
    conn.close()

"""The Claude status line tap and the `claude.statusline` source (C1)."""

import json
import os
import sqlite3
import stat
import sys

import pytest

from usage_watch import cli, identity, model
from usage_watch.collectors import statusline
from usage_watch.collectors.statusline import ClaudeStatuslineSource, tap
from usage_watch.runtime.core import Runtime, account_alias_value

SECRET = b"s" * 32
SID = "0f1e2d3c-aaaa-bbbb-cccc-123456789abc"
ACCT, ORG = "AAAA-1111", "BBBB-2222"

# Echo stdin back as stdout, exit 3: shows the tap passes all three through.
ECHO = [sys.executable, "-c",
        "import sys; d = sys.stdin.buffer.read(); sys.stdout.buffer.write(b'line:' + d); sys.exit(3)"]


def stdin(**extra):
    d = {
        "session_id": SID,
        "transcript_path": "/secret/path/transcript.jsonl",
        "cwd": "/home/me/private-project",
        "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
        "workspace": {"current_dir": "/home/me/private-project"},
        "rate_limits": {
            "five_hour": {"used_percentage": 42.5, "resets_at": 1_790_000_000, "note": "x"},
            "seven_day": {"used_percentage": 7, "resets_at": 1_790_500_000},
        },
    }
    d.update(extra)
    return json.dumps(d).encode()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # usage-watch may be installed on the machine running the tests; the
    # wrapper's prefix must not depend on that.
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    return home


def snap_path():
    return statusline.snapshot_dir() / f"{SID}.json"


def write_claude_json(home):
    (home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"accountUuid": ACCT, "organizationUuid": ORG, "emailAddress": "me@example.com"},
        "numStartups": 3,
    }))


# --- the tap -------------------------------------------------------------------------------

def test_tap_writes_snapshot_and_passes_everything_through():
    data = stdin()
    code, out = tap(ECHO, data)
    assert (code, out) == (3, b"line:" + data)
    snap = json.loads(snap_path().read_text())
    assert set(snap) == {"session_id", "rate_limits", "observed_at"}
    assert snap["session_id"] == SID
    assert snap["rate_limits"] == {
        "five_hour": {"used_percentage": 42.5, "resets_at": 1_790_000_000},
        "seven_day": {"used_percentage": 7, "resets_at": 1_790_500_000},
    }
    assert isinstance(snap["observed_at"], int) and snap["observed_at"] > 1_700_000_000_000
    assert not [p for p in statusline.snapshot_dir().iterdir() if p.name.startswith(".")]


def test_nothing_beyond_rate_limits_is_persisted(tmp_path):
    tap(ECHO, stdin())
    written = b"".join(p.read_bytes() for p in (tmp_path / "state").rglob("*") if p.is_file())
    for secret in (b"private-project", b"transcript", b"claude-opus", b"note", b"cwd", b"workspace"):
        assert secret not in written


def test_no_rate_limits_writes_nothing_but_still_runs():
    data = json.dumps({"session_id": SID, "cwd": "/x"}).encode()
    assert tap(ECHO, data) == (3, b"line:" + data)
    assert not statusline.snapshot_dir().exists()


@pytest.mark.parametrize("data", [b"", b"not json{", b"[1,2]", json.dumps(
    {"session_id": "../../etc/evil", "rate_limits": {"five_hour": {"used_percentage": 1}}}).encode()])
def test_tap_never_breaks_on_bad_stdin(data, tmp_path):
    assert tap(ECHO, data) == (3, b"line:" + data)
    assert not (tmp_path / "etc").exists()


def test_tap_never_breaks_when_state_dir_unwritable(monkeypatch, tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the state directory should be")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))
    data = stdin()
    assert tap(ECHO, data) == (3, b"line:" + data)


def test_tap_never_breaks_when_snapshot_dir_read_only(tmp_path):
    d = statusline.snapshot_dir()
    d.mkdir(parents=True)
    os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)
    try:
        data = stdin()
        assert tap(ECHO, data) == (3, b"line:" + data)
    finally:
        os.chmod(d, stat.S_IRWXU)


def test_no_command_prints_nothing_and_exits_zero():
    assert tap([], stdin()) == (0, b"")
    assert snap_path().exists()


def test_single_shell_string_runs_through_sh():
    code, out = tap(["printf a; printf b | tr b c"], b"{}")
    assert (code, out) == (0, b"ac")


def test_missing_command_is_127_not_a_crash():
    assert tap(["/no/such/command-usage-watch"], stdin()) == (127, b"")


def test_cli_statusline_tap_passes_remainder_unchanged(monkeypatch, capfdbinary):
    data = stdin()
    seen = {}

    def fake_tap(argv, stdin_bytes):
        seen["argv"], seen["stdin"] = argv, stdin_bytes
        return 5, b"out"

    class In:
        buffer = __import__("io").BytesIO(data)

        def isatty(self):
            return False

    monkeypatch.setattr(statusline, "tap", fake_tap)
    monkeypatch.setattr(sys, "stdin", In())
    assert cli.main(["statusline-tap", "--", "my-cmd", "--help", "-x", "--", "y"]) == 5
    assert seen == {"argv": ["my-cmd", "--help", "-x", "--", "y"], "stdin": data}
    assert capfdbinary.readouterr().out == b"out"


# --- the source ----------------------------------------------------------------------------

def test_source_emits_samples_and_account_evidence(_isolate):
    write_claude_json(_isolate)
    tap([], stdin(rate_limits={
        "five_hour": {"used_percentage": 42.5, "resets_at": 1_790_000_000},
        "seven_day": {"used_percentage": 7, "resets_at": 1_790_500_000},
        "spend_limit": {"used_percentage": 12},
    }))
    at = json.loads(snap_path().read_text())["observed_at"]
    src = ClaudeStatuslineSource(secret=SECRET)
    assert (src.name, src.primary, src.merge, src.interval_s) == ("claude.statusline", False, None, 10)
    items, wm = src.collect(None)
    samples = [i for i in items if isinstance(i, model.CapacitySample)]
    evidence = [i for i in items if isinstance(i, model.AttributionEvidence)]
    sk = f"claude:{SID}"
    assert samples == [
        model.CapacitySample(source="claude.statusline", stream_key=sk, window="session",
                             confidence="authoritative", observed_at=at, status="unknown",
                             used_pct=42.5, window_seconds=18000, resets_at=1_790_000_000_000),
        model.CapacitySample(source="claude.statusline", stream_key=sk, window="weekly",
                             confidence="authoritative", observed_at=at, status="unknown",
                             used_pct=7.0, window_seconds=604800, resets_at=1_790_500_000_000),
        model.CapacitySample(source="claude.statusline", stream_key=sk, window="other:spend_limit",
                             confidence="authoritative", observed_at=at, status="unknown",
                             used_pct=12.0, window_seconds=None, resets_at=None),
    ]
    value = account_alias_value("anthropic", "anthropic.account_org",
                                identity.account("anthropic", f"{ACCT}|{ORG}".lower(), SECRET))
    assert [(e.subject_kind, e.subject_id) for e in evidence] == [
        ("capacity_sample", f"claude.statusline|{sk}|{w}|{at}") for w in ("session", "weekly", "other:spend_limit")]
    assert {(e.dimension, e.value, e.method, e.source, e.confidence, e.validity) for e in evidence} == {
        ("account", value, "state_file", "claude.statusline", "inferred", "live")}
    assert json.loads(wm) == {f"{SID}.json": at}

    # unchanged snapshot: nothing new
    assert src.collect(wm) == ([], wm)


def test_source_emits_a_rewritten_snapshot_again(monkeypatch):
    statusline.write_snapshot(stdin(), now_ms=1000)
    src = ClaudeStatuslineSource(secret=SECRET)
    items, wm = src.collect(None)
    assert len(items) == 2  # no ~/.claude.json: samples only, no evidence
    statusline.write_snapshot(stdin(), now_ms=2000)
    items, wm2 = src.collect(wm)
    assert [i.observed_at for i in items] == [2000, 2000]
    assert json.loads(wm2) == {f"{SID}.json": 2000}


def test_source_without_snapshots_is_empty():
    assert ClaudeStatuslineSource(secret=SECRET).collect(None) == ([], None)


def test_source_skips_a_corrupt_snapshot():
    d = statusline.snapshot_dir()
    d.mkdir(parents=True)
    (d / "bad.json").write_text("{not json")
    assert ClaudeStatuslineSource(secret=SECRET).collect(None) == ([], None)


# --- the real runtime ----------------------------------------------------------------------

def test_through_the_runtime_into_a_store(tmp_path, _isolate):
    write_claude_json(_isolate)
    tap([], stdin())
    db = tmp_path / "usage.db"
    rt = Runtime([ClaudeStatuslineSource(interval_s=0)], db_path=db, lock_path=tmp_path / "run.lock")
    rt.start()
    try:
        rt.run_once()
        rt.run_once()  # unchanged snapshot: no duplicates
    finally:
        rt.stop()
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT window, used_pct, window_seconds, resets_at, stream_key"
                            " FROM capacity_samples ORDER BY window").fetchall()
        assert rows == [("session", 42.5, 18000, 1_790_000_000_000, f"claude:{SID}"),
                        ("weekly", 7.0, 604800, 1_790_500_000_000, f"claude:{SID}")]
        eff = conn.execute("SELECT subject_id, state, value FROM effective_attributions"
                           " WHERE subject_kind = 'capacity_sample' AND dimension = 'account'").fetchall()
        assert len(eff) == 2 and all(state == "attributed" and value for _, state, value in eff)
        assert not any(v.startswith("alias:") for _, _, v in eff)
        err = conn.execute("SELECT last_error FROM collector_status WHERE collector = 'claude.statusline'"
                           ).fetchone()
        assert err == (None,)
    finally:
        conn.close()


# --- init --claude-statusline --------------------------------------------------------------

def settings(home):
    return home / ".claude" / "settings.json"


def put_settings(home, data):
    settings(home).parent.mkdir(parents=True, exist_ok=True)
    settings(home).write_text(json.dumps(data, indent=2))


def test_init_shows_preview_and_does_not_apply_without_terminal(_isolate, capsys, monkeypatch):
    put_settings(_isolate, {"model": "opus", "statusLine": {"type": "command", "command": "~/.claude/sl.sh"}})
    before = settings(_isolate).read_bytes()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert cli.main(["init", "--claude-statusline"]) == 1
    cap = capsys.readouterr()
    assert '"command": "~/.claude/sl.sh"' in cap.out
    assert '"command": "usage-watch statusline-tap -- ~/.claude/sl.sh"' in cap.out
    assert "--yes" in cap.err
    assert settings(_isolate).read_bytes() == before


def test_init_applies_with_yes_undoes_and_refuses_double_wrap(_isolate, capsys):
    original = {"model": "opus", "statusLine": {"type": "command", "command": "~/.claude/sl.sh", "padding": 0}}
    put_settings(_isolate, original)
    before = settings(_isolate).read_bytes()
    assert cli.main(["init", "--claude-statusline", "--yes"]) == 0
    now = json.loads(settings(_isolate).read_text())
    assert now["model"] == "opus"
    assert now["statusLine"] == {"type": "command", "padding": 0,
                                 "command": "usage-watch statusline-tap -- ~/.claude/sl.sh"}
    backup = settings(_isolate).with_name("settings.json.usage-watch.bak")
    assert backup.read_bytes() == before

    capsys.readouterr()
    assert cli.main(["init", "--claude-statusline", "--yes"]) == 1
    assert "already runs through" in capsys.readouterr().err
    assert json.loads(settings(_isolate).read_text()) == now

    assert cli.main(["init", "--claude-statusline", "--undo"]) == 0
    assert settings(_isolate).read_bytes() == before
    assert not backup.exists()


def test_init_without_statusline_sets_bare_tap_and_undo_removes_file(_isolate):
    assert cli.main(["init", "--claude-statusline", "--yes"]) == 0
    assert json.loads(settings(_isolate).read_text()) == {
        "statusLine": {"type": "command", "command": "usage-watch statusline-tap --"}}
    assert cli.main(["init", "--claude-statusline", "--undo"]) == 0
    assert not settings(_isolate).exists()


def test_init_quotes_a_compound_command(_isolate):
    put_settings(_isolate, {"statusLine": {"type": "command", "command": "jq -r .model.id | cut -c1-5"}})
    assert cli.main(["init", "--claude-statusline", "--yes"]) == 0
    cmd = json.loads(settings(_isolate).read_text())["statusLine"]["command"]
    assert cmd == "usage-watch statusline-tap -- 'jq -r .model.id | cut -c1-5'"


def test_undo_without_backup_is_a_prompt(capsys):
    assert cli.main(["init", "--claude-statusline", "--undo"]) == 1
    assert "fix:" in capsys.readouterr().err


def test_undo_flag_alone_does_not_run_plain_init(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_WATCH_CONFIG", str(tmp_path / "c.toml"))
    assert cli.main(["init", "--undo"]) == 1
    assert "--claude-statusline" in capsys.readouterr().err
    assert not (tmp_path / "c.toml").exists()


def test_init_uses_the_absolute_path_when_usage_watch_is_found(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/opt/bin/usage-watch")
    assert cli.tap_prefix() == "/opt/bin/usage-watch statusline-tap --"
    assert cli.wrap_statusline_command("my-line", cli.tap_prefix()) == "/opt/bin/usage-watch statusline-tap -- my-line"

import re

from usage_watch import cli
from usage_watch.errors import Problem


def test_primer_explains_and_agent_primer_is_the_contract(capsys):
    assert cli.main(["primer"]) == 0
    full = capsys.readouterr().out
    for heading in ("What it is for", "How it works", "How it sees your setup", "Pane states",
                    "Guarantees", "Limits", "For agents"):
        assert f"## {heading}" in full
    assert cli.main(["primer", "--agent"]) == 0
    short = capsys.readouterr().out
    assert re.findall(r"(?m)^## (.+)$", short) == ["Guarantees", "Limits", "For agents"]
    assert len(short) < len(full) / 2 + 2000


def test_problem_renders_what_expected_fix():
    text = Problem("x failed", fix="do y\nthen z", expected="x works").render()
    assert text == "usage-watch: x failed\n  expected: x works\n  fix: do y\n       then z"


def test_bad_config_is_a_prompt(tmp_path, capsys, monkeypatch):
    # `status` reads only the store now, so `doctor` is the command that reads config.
    path = tmp_path / "c.toml"
    path.write_text("[defaults\n")
    monkeypatch.setenv("USAGE_WATCH_CONFIG", str(path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert cli.main(["doctor"]) == 1
    err = capsys.readouterr().err
    assert "not valid TOML" in err and "fix:" in err


def test_sanitize_removes_home_ids_and_emails():
    import os
    text = f'{os.path.expanduser("~")}/x "request_id":"req_123" me@example.org'
    assert cli.sanitize(text) == '~/x "request_id":"req_REDACTED" user@example.com'

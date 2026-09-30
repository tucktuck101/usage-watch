import datetime as dt
from pathlib import Path

import pytest

from usage_watch.adapters import by_name, for_process
from usage_watch.adapters.base import model_family, next_clock_time, visible_text

FIXTURES = Path(__file__).parent / "fixtures"


def read(harness, fixture, styled=""):
    return by_name(harness).read((FIXTURES / f"{fixture}.txt").read_text(), styled)


@pytest.mark.parametrize("harness,fixture,state", [
    ("omp", "omp_stalled_todo", "stalled"),
    ("omp", "omp_stalled_plain", "stalled"),
    ("omp", "omp_owner_typing", "typing"),
    ("omp", "omp_resumed_idle", "idle"),
    ("omp", "omp_busy", "busy"),
    ("omp", "omp_idle", "idle"),
    ("claude", "claude_busy", "busy"),
    ("claude", "claude_idle", "idle"),
    ("claude", "claude_typing", "typing"),
    ("claude", "claude_idle_discussing_limits", "idle"),
    ("claude", "claude_stalled", "stalled"),
    ("claude", "claude_resuming", "resuming"),
    ("claude", "claude_context_limit", "idle"),
    ("codex", "codex_stalled", "stalled"),
    ("codex", "codex_busy", "busy"),
    ("codex", "codex_idle", "idle"),
])
def test_state(harness, fixture, state):
    assert read(harness, fixture).state == state


def test_omp_stall_is_keyed_by_request_id():
    r = read("omp", "omp_stalled_todo")
    assert r.error_key == "req_REDACTED_A"
    assert r.model == "Fable 5.1"


def test_omp_model_and_family():
    assert read("omp", "omp_busy").model == "Sonnet 5"
    assert model_family("Sonnet 5") == "claude"
    assert model_family("gpt-6-sol") == "codex"
    assert model_family("DeepSeek V4") is None


def test_claude_reset_hint_and_model():
    r = read("claude", "claude_stalled")
    assert r.reset_hint is not None and r.reset_hint.hour == 15
    assert r.model == "Opus 5.5"


def test_codex_reset_hint():
    r = read("codex", "codex_stalled")
    assert (r.reset_hint.hour, r.reset_hint.minute) == (22, 29)


def test_same_screen_same_key_different_screen_different_key():
    assert read("claude", "claude_stalled").error_key == read("claude", "claude_stalled").error_key
    assert read("codex", "codex_stalled").error_key != read("claude", "claude_stalled").error_key


def test_unrecognised_screen_is_unknown_for_every_adapter():
    for name in ("omp", "claude", "codex"):
        assert by_name(name).read("just a shell prompt\n$ ").state == "unknown"


def test_dim_placeholder_is_not_typing():
    plain = (FIXTURES / "claude_idle.txt").read_text().replace("❯\n", '❯ Try "fix lint errors"\n')
    styled = '\x1b[39m❯ \x1b[2mTry "fix lint errors"\x1b[22m\n'
    assert by_name("claude").read(plain, styled).state == "idle"
    assert by_name("claude").read(plain).state == "typing"  # without styles, assume typed


def test_visible_text_drops_dim_and_grey_runs():
    assert visible_text("a\x1b[2mdim\x1b[22mb\x1b[90mgrey\x1b[39mc") == "abc"


def test_next_clock_time_rolls_to_tomorrow():
    now = dt.datetime(2026, 9, 30, 23, 0).astimezone()
    t = next_clock_time("10:29 PM", now)
    assert (t.day, t.hour, t.minute) == (1, 22, 29)
    assert next_clock_time("3pm", now).hour == 15
    assert next_clock_time("soon", now) is None


def test_process_matching():
    assert for_process("omp", "omp").name == "omp"
    assert for_process("claude", "claude --resume").name == "claude"
    assert for_process("codex", "codex --yolo").name == "codex"
    assert for_process("node", "node /usr/local/lib/node_modules/@openai/codex/bin/codex.js").name == "codex"
    assert for_process("zsh", "-zsh") is None

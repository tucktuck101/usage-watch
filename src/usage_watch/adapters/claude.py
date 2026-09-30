"""Claude Code.

The input box is a `❯` line between two rules, with a model and mode footer
below. While working, a spinner line such as `✳ Bloviating… (55s · ↓ 2.6k
tokens)` sits above the box.

Limit wording comes from the Claude Code 2.1.285 binary: `Usage limit
reached`, `You've hit your … limit`, `You've reached your … limit`. Claude Code
can also resume by itself (`continuing automatically … esc to cancel`), and
such a pane is left alone.
"""

import re

from .base import Adapter, Reading, key_of, lines_of, next_clock_time, visible_text

RULE = re.compile(r"^\s*─{8,}\s*$")
SPINNER = re.compile(r"^\s*\S\s+\S+…\s+\(\d+[smh]|esc to interrupt")
LIMIT = re.compile(
    r"usage limit reached|you've hit your [\w\- ]*limit|you've reached your [\w\- ]*limit"
    r"|(fable|opus|sonnet|weekly|session) limit reached",
    re.I,
)
SELF_RESUME = re.compile(r"continuing automatically|continues automatically when it resets", re.I)
RESETS = re.compile(r"resets? (?:at )?(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)", re.I)
MODEL = re.compile(r"\b(Opus|Sonnet|Haiku|Fable)\s+[\d.]+", re.I)


class Claude(Adapter):
    name = "claude"
    verified = "busy screen and footer from a live pane; limit wording from the 2.1.285 binary; no live stall recorded yet"

    def matches(self, argv0, args):
        return argv0 == "claude"

    def family(self, model):
        return "claude"

    def read(self, plain, styled=""):
        lines = lines_of(plain)
        prompt = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].lstrip().startswith("❯")), None)
        if prompt is None or prompt == 0 or not RULE.match(lines[prompt - 1]):
            return Reading("unknown", note="no Claude Code input box on screen")
        m = MODEL.search("\n".join(lines[prompt:]))
        model = m.group(0) if m else None
        if composer_text(plain, styled, lines[prompt]):
            return Reading("typing", model)
        above = lines[: prompt - 1][-12:]
        if any(SPINNER.search(line) for line in above[-4:]):
            return Reading("busy", model)
        # The notice sits directly above the input box. Looking further up would
        # catch a transcript that merely talks about limits.
        near = above[-5:]
        text = "\n".join(near)
        hit = [line for line in near if LIMIT.search(line) and "context limit" not in line.lower()]
        if not hit:
            return Reading("idle", model)
        if SELF_RESUME.search(text):
            return Reading("resuming", model, note="Claude Code will continue by itself")
        r = RESETS.search(text)
        return Reading(
            "stalled", model,
            error_key=key_of("claude", *hit),
            reset_hint=next_clock_time(r.group(1)) if r else None,
        )


def composer_text(plain: str, styled: str, prompt_line: str) -> str:
    """What is typed after `❯`, ignoring dim placeholder text when styles are known."""
    if styled:
        styled_lines = [line for line in styled.splitlines() if "❯" in line]
        if styled_lines:
            return visible_text(styled_lines[-1]).split("❯", 1)[1].strip()
    return prompt_line.split("❯", 1)[1].strip()

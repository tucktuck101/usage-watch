"""Codex CLI.

Limit wording is confirmed from a real session log and from the codex-cli
0.154.0 binary: `You've hit your usage limit. … try again at 10:29 PM.` and
the per-model form `You've hit your usage limit for <model>.`

The busy and input-box layout has not been seen on a live pane. The adapter
therefore reads `stalled` only when the limit message is the last thing above
an empty `›` input line and nothing on screen says it is working. Anything
else reads `unknown`, never guessed.
"""

import re

from .base import Adapter, Reading, key_of, lines_of, next_clock_time, visible_text

LIMIT = re.compile(r"you've hit your usage limit", re.I)
BUSY = re.compile(r"esc to interrupt|^\s*[•◦]\s*Working\b", re.I)
TRY_AGAIN = re.compile(r"try again at ((?:[A-Z][a-z]{2} \d{1,2}, \d{4} )?\d{1,2}:\d{2}\s*[AP]M)", re.I)
MODEL_FOR = re.compile(r"usage limit for ([\w.\-]+)", re.I)


class Codex(Adapter):
    name = "codex"
    verified = "limit wording from a real session log and the 0.154.0 binary; screen layout unverified"

    def matches(self, argv0, args):
        return argv0 == "codex" or re.search(r"(^|/)codex(\.js)?(\s|$)", args) is not None

    def family(self, model):
        return "codex"

    def read(self, plain, styled=""):
        lines = lines_of(plain)
        prompt = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].lstrip().startswith("›")), None)
        if prompt is None:
            return Reading("unknown", note="no Codex input line on screen")
        typed = lines[prompt].split("›", 1)[1].strip()
        if styled:
            styled_lines = [line for line in styled.splitlines() if "›" in line]
            if styled_lines:
                typed = visible_text(styled_lines[-1]).split("›", 1)[1].strip()
        if typed:
            return Reading("typing", note="text in the input line (placeholder styling unverified)")
        above = lines[:prompt][-10:]
        if any(BUSY.search(line) for line in above):
            return Reading("busy")
        near = above[-5:]  # the notice sits directly above the input line
        hit = [line for line in near if LIMIT.search(line)]
        if not hit:
            return Reading("idle")
        text = "\n".join(near)
        m = MODEL_FOR.search(text)
        r = TRY_AGAIN.search(text)
        return Reading(
            "stalled", m.group(1) if m else None,
            error_key=key_of("codex", *hit),
            reset_hint=next_clock_time(r.group(1)) if r else None,
        )

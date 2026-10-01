"""What every harness adapter answers about a pane's screen.

An adapter reads the visible screen and says one of:

  busy          the agent is working
  idle          waiting for input, not because of a limit
  stalled       stopped on a usage limit and waiting for someone to type
  resuming      stopped on a limit, but the harness will continue by itself
  typing        someone has text in the input box; never type over it
  unknown       the screen did not match what the adapter knows

Anything the adapter is unsure of is `unknown`, and `unknown` is never nudged.
"""

import datetime as dt
import hashlib
import re
from dataclasses import dataclass

STATES = ("busy", "idle", "stalled", "resuming", "typing", "unknown")


@dataclass
class Reading:
    state: str
    model: str | None = None        # model name as the screen shows it
    error_key: str | None = None    # identifies one stall, so it is nudged once
    reset_hint: dt.datetime | None = None  # when the screen says the limit lifts
    note: str = ""
    retry_after_ms: int | None = None  # the provider's own wait, relative to the stall's onset


class Adapter:
    name = ""
    verified = ""  # what the adapter's patterns were checked against

    def matches(self, argv0: str, args: str) -> bool:
        raise NotImplementedError

    def read(self, plain: str, styled: str = "") -> Reading:
        raise NotImplementedError

    def family(self, model: str | None) -> str | None:
        return model_family(model)


def lines_of(text: str) -> list[str]:
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def model_family(model: str | None) -> str | None:
    if not model:
        return None
    m = model.lower()
    if re.search(r"opus|sonnet|haiku|fable|claude", m):
        return "claude"
    if re.search(r"gpt|codex|\bo\d", m):
        return "codex"
    return None


def key_of(*parts: str) -> str:
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:12]


_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_OTHER_ESC = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def visible_text(styled_line: str) -> str:
    """Text of a styled line minus dim or grey runs, which are placeholders."""
    out, dim, pos = [], False, 0
    for m in _SGR.finditer(styled_line):
        if not dim:
            out.append(styled_line[pos:m.start()])
        codes = [c for c in m.group(1).split(";") if c] or ["0"]
        for c in codes:
            if c in ("0", "22", "39"):
                dim = False
            elif c in ("2", "90"):
                dim = True
        pos = m.end()
    if not dim:
        out.append(styled_line[pos:])
    return _OTHER_ESC.sub("", "".join(out))


def next_clock_time(text: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """Parse '10:29 PM', '3pm' or 'Oct 2, 2026 10:29 PM' into the next such local time."""
    now = now or dt.datetime.now().astimezone()
    s = text.strip().upper().replace(".", "")
    for fmt in ("%b %d, %Y %I:%M %p", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p"):
        try:
            t = dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
        if t.year == 1900:
            t = t.replace(year=now.year, month=now.month, day=now.day)
            t = t.replace(tzinfo=now.tzinfo)
            return t if t > now else t + dt.timedelta(days=1)
        return t.replace(tzinfo=now.tzinfo)
    return None

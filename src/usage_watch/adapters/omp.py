"""omp (oh-my-pi).

On a 429 whose requested wait exceeds `retry.maxDelayMs`, omp prints the
error, gives up, and returns to an idle prompt. Nothing wakes it again.

Idle and busy show in the composer header: `╭── π > …` when idle, and a
spinner with an elapsed time (`╭── ⠴ 18s > …`) when working.
"""

import re

from .base import Adapter, Reading, key_of, lines_of

ERROR = re.compile(r"^\s*Error: .*(429|rate_limit_error|usage limit)", re.I)
CONTINUATION = re.compile(r"rate_limit_error|retry-after-ms|request_id|^\s*later\.|^\s*\{", re.I)
REQUEST_ID = re.compile(r'"request_id":"([^"]+)"')
IDLE_HEADER = re.compile(r"^╭──\s*π\s*>")
MODEL = re.compile(r"[◒◔◑◐○●◕◓◌]\s+([A-Za-z][\w.\- ]*?)\s+>")
BOX = "╭╮╰╯│├└┌┐┘─┃╎▶◀ "


class Omp(Adapter):
    name = "omp"
    verified = "live stalled, busy and idle panes (omp, 2026-09-30)"

    def matches(self, argv0, args):
        return argv0 == "omp"

    def read(self, plain, styled=""):
        lines = lines_of(plain)
        header = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith("╭──")), None)
        if header is None:
            return Reading("unknown", note="no omp composer on screen")
        m = MODEL.search(lines[header])
        model = m.group(1).strip() if m else None
        if not IDLE_HEADER.match(lines[header]):
            return Reading("busy", model)
        if any(line.strip(BOX) for line in lines[header + 1:]):
            return Reading("typing", model)
        body = lines[:header]
        anchor = next((i for i in range(len(body) - 1, -1, -1) if ERROR.match(body[i])), None)
        if anchor is None:
            return Reading("idle", model)
        # After the error, only its own continuation, the todo widget and box
        # drawing may follow. Anything else means the agent moved on.
        for line in body[anchor + 1:]:
            s = line.strip()
            if CONTINUATION.search(s) or s == "TODO" or s[0] in BOX:
                continue
            return Reading("idle", model)
        tail = "\n".join(body[max(0, anchor - 3):])
        ids = REQUEST_ID.findall(tail)
        return Reading("stalled", model, error_key=ids[-1] if ids else key_of(body[anchor]))

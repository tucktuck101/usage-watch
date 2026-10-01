"""The `screen` collector (D6): today's pane adapters as a pull source.

Each pass scans the topology, reads every agent pane with its adapter
(`screen.capture`, then the pane's `Adapter.read`),
and emits one `AgentStateSample` per pane. A stall occurrence (D8) also
emits one `LimitEvent` when it begins.

Screen text is parsed in memory and never stored (D4, D5): a sample's
`note` is the adapter's fixed explanation, and the watermark holds only
error keys, onsets and pane PIDs. No network calls.

The watermark is a JSON object, pane id -> {error_key, onset, pane_pid},
for the panes currently stalled. It carries stall onset across restarts.

Readings of the design that this module leaves open are marked "Reading:".
"""

from __future__ import annotations

import json
import time
from typing import Callable

from .. import identity, screen, topology
from ..errors import Problem
from ..model import AgentStateSample, LimitEvent

__all__ = ["ScreenSource", "stall_id", "stream_raw"]

SOURCE = "screen"


def stream_raw(pane_id: str, pane_pid: int) -> str:
    """D4: pane ID plus pane PID, so a reused pane ID can't collide."""
    return f"{pane_id}:{pane_pid}"


def stall_id(raw: str, error_key: str, onset: int, secret: bytes | None = None) -> str:
    """D8: one stall occurrence, from (the pane, error_key, stall onset)."""
    return identity.stream(f"{raw}|{error_key}|{onset}", secret)


def _load(watermark: str | None) -> dict[str, dict]:
    """The tracked stalls. A missing or unreadable watermark starts empty."""
    if not watermark:
        return {}
    try:
        data = json.loads(watermark)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for pane, entry in data.items():
        if (isinstance(entry, dict) and isinstance(entry.get("onset"), int)
                and isinstance(entry.get("pane_pid"), int)
                and isinstance(entry.get("error_key", ""), (str, type(None)))):
            out[pane] = {"error_key": entry.get("error_key"), "onset": entry["onset"],
                         "pane_pid": entry["pane_pid"]}
    return out


def _ms(t) -> int | None:
    return int(t.timestamp() * 1000) if t is not None else None


class ScreenSource:
    """A `PullSource` over tmux panes. Never primary; emits no observations."""

    name = SOURCE
    primary = False
    merge = None

    def __init__(self, interval_s: float = 5, clock: Callable[[], float] = time.time,
                 secret: bytes | None = None):
        self.interval_s = interval_s
        self.clock = clock
        self.secret = secret

    def collect(self, watermark: str | None) -> tuple[list, str | None]:
        tracked = _load(watermark)
        now = int(self.clock() * 1000)  # the scan's own time (D4)
        items: list = []
        still: dict[str, dict] = {}

        for pane in topology.scan().agents:
            prior = tracked.get(pane.id)
            if prior is not None and prior["pane_pid"] != pane.pid:
                prior = None  # a new PID is a new pane
            try:
                plain, styled = screen.capture(pane.id)
                reading = pane.harness.read(plain, styled)
            except Problem:
                # Skipped, not fatal. Reading: an unread pane was not seen not
                # stalled, so a stall it was in stays tracked.
                if prior is not None:
                    still[pane.id] = prior
                continue

            # The provider's own retry wait is relative to when the stall
            # began, so it is pinned to the onset; recomputing it from each
            # re-read would push it later forever.
            same = (reading.state == "stalled" and prior is not None
                    and prior["error_key"] == reading.error_key)
            onset = prior["onset"] if same else now
            reset_ms = _ms(reading.reset_hint)
            if (reset_ms is None and reading.state == "stalled"
                    and getattr(reading, "retry_after_ms", None)):
                reset_ms = onset + reading.retry_after_ms
            items.append(AgentStateSample(
                pane=pane.id, observed_at=now, state=reading.state, source=SOURCE,
                harness=pane.harness.name, session_key=None, model=reading.model,
                reset_hint=reset_ms, error_key=reading.error_key,
                note=reading.note or None, confidence="inferred",
            ))

            if reading.state == "unknown":
                # Reading: `unknown` says nothing about the stall, so it
                # neither ends nor starts one.
                if prior is not None:
                    still[pane.id] = prior
                continue
            if reading.state != "stalled":
                continue  # recovered: dropped from the watermark

            if prior is not None and prior["error_key"] == reading.error_key:
                still[pane.id] = prior  # the same occurrence, seen again
                continue

            # Reading: a different error_key while still stalled is a new
            # occurrence ("the first sample showing a stall with that error_key").
            entry = {"error_key": reading.error_key, "onset": now, "pane_pid": pane.pid}
            still[pane.id] = entry
            raw = stream_raw(pane.id, pane.pid)
            items.append(LimitEvent(
                source=SOURCE,
                stream_key=identity.stream(raw, self.secret),
                source_key=stall_id(raw, reading.error_key or "", now, self.secret),
                kind="hit", confidence="inferred", observed_at=now,
                resets_at=reset_ms,
            ))

        # Panes that are gone are not carried over.
        return items, json.dumps(still, sort_keys=True)

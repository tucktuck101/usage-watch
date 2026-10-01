"""Pool alerts (plan item A1): tell the user when an account's window runs low.

For each account and window, the newest anchor within its source's display
age (D6) is read. When its remaining share (100 - used_pct) is at or below a
threshold, a message is returned, and passed to `notify`, once per
(account, window, resets_at, threshold). What has fired is kept in memory
only, so a restart may repeat an alert once.

Readings of the design that this module leaves open are marked "Reading:".
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time

from . import store
from .policy import DISPLAY_AGE_MS, account_anchors

__all__ = ["Alerts"]


class Alerts:
    def __init__(self, conn: sqlite3.Connection, *, thresholds=(20, 5), clock=time.time,
                 notify=None):
        self.conn = conn
        self.thresholds = sorted(thresholds)  # most severe first
        self.clock = clock
        if notify is None:
            from .watcher import notify  # imported late: watcher pulls in the old pool code
        self.notify = notify
        self._fired: set[tuple] = set()

    def check(self) -> list[str]:
        if not store.schema_status(self.conn).readable:
            return []
        now = int(self.clock() * 1000)
        newest: dict[tuple[str, str], object] = {}
        for a in account_anchors(self.conn, None, now - max(DISPLAY_AGE_MS.values()),
                                 set(DISPLAY_AGE_MS)):
            if now - a.observed_at > DISPLAY_AGE_MS[a.source]:
                continue
            newest.setdefault((a.account, a.window), a)  # newest first
        labels = dict(self.conn.execute("SELECT account_key, label FROM accounts"))
        messages = []
        for (account, window), a in sorted(newest.items()):
            rem = a.remaining()
            # Reading: an anchor of an ended window instance says nothing about now.
            if rem is None or (a.resets_at is not None and a.resets_at <= now):
                continue
            crossed = [t for t in self.thresholds if rem <= t]
            if not crossed:
                continue
            keys = [(account, window, a.resets_at, t) for t in crossed]
            if all(k in self._fired for k in keys):
                continue
            # Reading: one message for the most severe threshold crossed; the
            # milder ones it implies are marked fired with it.
            self._fired.update(keys)
            who = labels.get(account) or account[:8]
            when = (dt.datetime.fromtimestamp(a.resets_at / 1000).astimezone().strftime("%a %H:%M")
                    if a.resets_at is not None else "unknown")
            msg = (f"{who}: {window} window has {rem:g}% left (at or below {crossed[0]:g}%); "
                   f"resets {when} [{a.source}]")
            messages.append(msg)
            self.notify("usage-watch", msg)
        return messages

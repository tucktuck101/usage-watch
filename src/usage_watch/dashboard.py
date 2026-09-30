"""A terminal dashboard: every pool's limits, and every agent's state, refreshing.

Agents are rescanned every few seconds, and pools are reread less often,
since the usage source caches for minutes anyway. With --watch it also
nudges, under the same rules and the same one-per-machine lock as `run`.
"""

import curses
import datetime as dt
import sys
import threading
import time

from . import config, watcher
from .errors import Problem
from .pool import Pools, parse_time
from .topology import scan

STATE_COLOR = {"busy": 1, "idle": 0, "stalled": 3, "resuming": 2, "typing": 4, "unknown": 5}


def bar(left: float | None, width: int = 12) -> str:
    if left is None:
        return " " * width
    filled = round(max(0, min(100, left)) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def until(t: dt.datetime | None) -> str:
    if not t:
        return ""
    s = int((t - dt.datetime.now(dt.timezone.utc)).total_seconds())
    if s <= 0:
        return "now"
    h, m = divmod(s // 60, 60)
    return f"{h // 24}d{h % 24}h" if h >= 24 else f"{h}h{m:02d}m" if h else f"{m}m"


def pool_lines(pools: Pools) -> list[tuple[str, int]]:
    """(text, color) per pool window: session, weekly, then any model-specific pool."""
    lines = []
    for pid, info in sorted(pools.providers().items()):
        res = info.get("resources", {})
        windows = [k for k in ("session", "weekly") if k in res]
        windows += [k for k, r in res.items() if k not in windows and r.get("kind") == "consumption"]
        head = f"{pid:18} {info.get('plan', '')[:10]:10}"
        for i, name in enumerate(windows):
            r = res[name]
            left = r.get("remaining")
            reset = parse_time(r.get("resetsAt"))
            color = 3 if left is not None and left <= 5 else 2 if left is not None and left <= 25 else 1
            when = f"resets in {until(reset)}" if reset else ""
            label = head if i == 0 else " " * len(head)
            pct = f"{left:>3.0f}% left" if left is not None else "   ?"
            lines.append((f"{label}  {name:8} {bar(left)} {pct}  {when}", color))
    for e in pools.errors():
        lines.append((f"{e.get('providerId', '?'):18} error: {e.get('message', '')}", 5))
    return lines


def agent_lines(observations) -> list[tuple[str, int]]:
    rows = [("PANE    HARNESS  ROLE          PROJECT             MODEL        STATE     ACTION", 6)]
    for o in observations:
        p = o.pane
        project = (p.lane or (p.project or "").rsplit("/", 1)[-1] or "-")[:18]
        action = o.action if o.action != "none" else ""
        if o.action == "wait" and o.capacity:
            action = f"wait: {o.capacity.why}"
        rows.append((f"{p.id:7} {p.harness.name:8} {p.role:13} {project:19} {(o.reading.model or '-')[:12]:12} "
                     f"{o.reading.state:9} {action}", STATE_COLOR.get(o.reading.state, 0)))
    if len(rows) == 1:
        rows.append(("no agent panes found in tmux", 5))
    return rows


def draw(win, pools_view, agents_view, events, status):
    win.erase()
    h, w = win.getmaxyx()
    y = 0

    def put(text, color=0, attr=0):
        nonlocal y
        if y < h:
            win.addnstr(y, 0, text, max(0, w - 1), curses.color_pair(color) | attr)
        y += 1

    put(status, 6, curses.A_BOLD)
    put("")
    put("POOLS", 6, curses.A_BOLD)
    for text, color in pools_view:
        put(text, color)
    put("")
    put("AGENTS", 6, curses.A_BOLD)
    for text, color in agents_view:
        put(text, color)
    if events:
        put("")
        put("EVENTS", 6, curses.A_BOLD)
        for text in events[-(max(1, h - y - 1)):]:
            put(text, 5)
    win.refresh()


class Worker(threading.Thread):
    """Scans and reads pools off the screen loop, so keys never wait on a scan."""

    def __init__(self, cfg, scan_every: float, pool_every: float, watch: bool):
        super().__init__(daemon=True)
        self.cfg, self.scan_every, self.pool_every, self.watch = cfg, scan_every, pool_every, watch
        self.stop, self.wake = threading.Event(), threading.Event()
        self.force = False
        self.pools_view: list = []
        self.agents_view: list = [("scanning…", 5)]
        self.events: list[str] = []
        self.problem = ""
        self.memory = watcher.Memory()
        self.log = watcher.Log(to_file=watch)

    def run(self):
        pools, pools_at = Pools(), 0.0
        while not self.stop.is_set():
            force, self.force = self.force, False
            try:
                if force or time.monotonic() - pools_at >= self.pool_every:
                    pools, pools_at = Pools(), time.monotonic()
                    if force:
                        pools.load(force=True)
                    self.pools_view = pool_lines(pools)
                observations = watcher.observe(scan(), self.cfg, pools, self.memory)
                self.agents_view = agent_lines(observations)
                if self.watch:
                    self.act(observations)
                self.problem = ""
            except Problem as e:
                self.problem = e.what
            self.wake.wait(self.scan_every)
            self.wake.clear()

    def act(self, observations):
        for o in observations:
            if o.action == "nudge":
                msg = f"{o.pane.id} {o.pane.harness.name}: {watcher.nudge(o, self.cfg, self.memory, settle=0)}"
            elif o.action == "escalate" and o.pane.id not in self.memory.escalated:
                self.memory.escalated.add(o.pane.id)
                msg = f"ESCALATE {o.reason.splitlines()[0]}"
                watcher.notify("usage-watch", msg)
            else:
                continue
            self.log(msg)
            self.events.append(f"{dt.datetime.now():%H:%M:%S} {msg}")


def loop(win, worker: Worker):
    curses.curs_set(0)
    curses.use_default_colors()
    for n, c in ((1, curses.COLOR_GREEN), (2, curses.COLOR_YELLOW), (3, curses.COLOR_RED),
                 (4, curses.COLOR_CYAN), (5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE), (6, -1)):
        curses.init_pair(n, c, -1)
    win.timeout(250)
    worker.start()
    mode = "watching and nudging" if worker.watch else "view only (--watch to nudge)"
    while True:
        status = f"usage-watch  {dt.datetime.now():%H:%M:%S}  {mode}  ·  r refresh  q quit"
        if worker.problem:
            status += f"  ·  ERROR {worker.problem}"
        draw(win, worker.pools_view, worker.agents_view, worker.events, status)
        key = win.getch()
        if key in (ord("q"), ord("Q"), 27):
            worker.stop.set()
            worker.wake.set()
            return
        if key in (ord("r"), ord("R")):
            worker.force = True
            worker.wake.set()


def run(scan_every: float = 5, pool_every: float = 60, watch: bool = False) -> None:
    if not sys.stdout.isatty():
        raise Problem("the dashboard needs an interactive terminal",
                      fix="run it in a terminal, or use `usage-watch status --json` from scripts")
    cfg = config.load()
    lock = watcher.acquire_lock() if watch else None
    try:
        curses.wrapper(loop, Worker(cfg, scan_every, pool_every, watch))
    finally:
        if lock:
            lock.close()

"""A terminal dashboard: every pool's limits, and every agent's state, refreshing.

It reads the store (F4): pools from `queries.pools`, each with its age and a
stale mark; agents from `queries.agents`, with the nudge policy's decision
for each, in view-only mode; and the collector's liveness line. With
--watch it also hosts the collector runtime and the nudge policy, like
`run`, under the same one-per-machine lock.

It records its own look (D3): `opened_at` on start, `last_seen_at` on each
refresh, `closed_at` on a clean exit (q, Esc or Ctrl-C).
"""

import curses
import datetime as dt
import sys
import threading
import time

from . import config, queries, store, watcher
from .errors import Problem
from .runtime.core import default_lock_path
from .runtime.liveness import liveness, lock_held

STATE_COLOR = {"busy": 1, "idle": 0, "stalled": 3, "resuming": 2, "typing": 4, "unknown": 5}
LOOK_WHO = LOOK_VIEW = "dashboard"


def bar(left: float | None, width: int = 12) -> str:
    if left is None:
        return " " * width
    filled = round(max(0, min(100, left)) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def until(t: dt.datetime | None, now: dt.datetime | None = None) -> str:
    if not t:
        return ""
    s = int((t - (now or dt.datetime.now(dt.timezone.utc))).total_seconds())
    if s <= 0:
        return "now"
    h, m = divmod(s // 60, 60)
    return f"{h // 24}d{h % 24}h" if h >= 24 else f"{h}h{m:02d}m" if h else f"{m}m"


def until_ms(ms: int | None, now_ms: int) -> str:
    if ms is None:
        return ""
    utc = dt.timezone.utc
    return until(dt.datetime.fromtimestamp(ms / 1000, utc), dt.datetime.fromtimestamp(now_ms / 1000, utc))


def pool_lines(pools: list[dict], now_ms: int) -> list[tuple[str, int]]:
    """(text, color) per pool window, from `queries.pools` rows. A stale
    reading is shown dim, with its age and STALE, never as current (D6)."""
    lines = []
    last = None
    for p in pools:
        left = p["remaining_pct"]
        color = 3 if left is not None and left <= 5 else 2 if left is not None and left <= 25 else 1
        if p["stale"]:
            color = 5
        head = p["account_label"] if p["account_label"] != last else ""
        last = p["account_label"]
        pct = f"{left:>3.0f}% left" if left is not None else "   ?    "
        reset = until_ms(p["resets_at"], now_ms)
        when = f"resets in {reset}" if reset else ""
        age = f"{p['source']} {p['age']} ago" + ("  STALE" if p["stale"] else "")
        lines.append((f"{head[:28]:28} {p['window'][:14]:14} {bar(left)} {pct}  {when:18} {age}", color))
    if not lines:
        lines.append(("no capacity readings in the store yet", 5))
    return lines


def agent_lines(agents: list[dict], decisions: dict | None = None) -> list[tuple[str, int]]:
    """(text, color) per agent, from `queries.agents` rows, with the policy's
    decision for that pane where there is one (D8: the reason is shown)."""
    decisions = decisions or {}
    rows = [("PANE    HARNESS  STATE     MODEL        ACCOUNT                   ACTION", 6)]
    for g in agents:
        d = decisions.get(g["pane"])
        action = ""
        if d is not None and d.action != "none":
            action = d.action + (f": {d.reason}" if d.reason else "")
        account = g["account_label"] or (g["account_state"] or "-")
        rows.append((f"{g['pane']:7} {(g['harness'] or '-')[:8]:8} {g['state']:9} "
                     f"{(g['model'] or '-')[:12]:12} {account[:25]:25} {action}",
                     STATE_COLOR.get(g["state"], 0)))
    if len(rows) == 1:
        rows.append(("no agent panes seen in the last 2 minutes", 5))
    return rows


def draw(win, pools_view, agents_view, events, status, live=""):
    win.erase()
    h, w = win.getmaxyx()
    y = 0

    def put(text, color=0, attr=0):
        nonlocal y
        if y < h:
            win.addnstr(y, 0, text, max(0, w - 1), curses.color_pair(color) | attr)
        y += 1

    put(status, 6, curses.A_BOLD)
    if live:
        put(live, 5)
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
    """Reads the store off the screen loop, so keys never wait on a read.
    With `watch`, it also hosts the collector runtime and the nudge policy.
    Every connection is opened and used on this thread."""

    def __init__(self, cfg, scan_every: float, watch: bool, clock=time.time):
        super().__init__(daemon=True)
        self.cfg, self.scan_every, self.watch, self.clock = cfg, scan_every, watch, clock
        self.stop, self.wake = threading.Event(), threading.Event()
        self.clean = False  # set with stop on q, Esc or Ctrl-C: the look closes
        self.pools_view: list = []
        self.agents_view: list = [("reading…", 5)]
        self.live = ""
        self.events: list[str] = []
        self.problem = ""
        self.file_log = watcher.Log(to_file=watch).path

    def log(self, msg: str) -> None:
        """An event on screen and, with --watch, in the log file; never on
        stdout, which curses owns."""
        self.events.append(f"{dt.datetime.now():%H:%M:%S} {msg}")
        if self.file_log:
            with open(self.file_log, "a") as f:
                f.write(f"{dt.datetime.now().astimezone():%Y-%m-%d %H:%M:%S} {msg}\n")

    def run(self):
        from .cli import Host, policy_class
        host = viewer = conn = rw = None
        try:
            if self.watch:
                host = Host(self.cfg, act=True, log=self.log)
                host.start()
            while not self.stop.is_set():
                try:
                    decisions = host.tick() if host else None
                    if conn is None and store.default_path().exists():
                        conn = store.connect(readonly=True)
                        Policy = policy_class()
                        if Policy is not None and not self.watch:
                            # View only: the policy decides but never types,
                            # on a read-only connection (D3: the dashboard
                            # writes only its own looks row).
                            viewer = Policy(conn, self.cfg, act=False)
                        rw = store.connect()
                        queries.open_look(rw, LOOK_WHO, LOOK_VIEW, int(self.clock() * 1000))
                    if viewer is not None:
                        try:
                            decisions = viewer.tick()
                        except Exception as e:
                            decisions = None
                            self.log(f"nudge policy (view only) failed: {type(e).__name__}")
                    if self.refresh(conn, decisions) and rw is not None:
                        queries.touch_look(rw, LOOK_WHO, LOOK_VIEW, int(self.clock() * 1000))
                except Problem as e:
                    self.problem = e.what
                self.wake.wait(self.scan_every)
                self.wake.clear()
            if self.clean and rw is not None:
                queries.close_look(rw, LOOK_WHO, LOOK_VIEW, int(self.clock() * 1000))
        except Problem as e:
            self.problem = e.what
        finally:
            for c in (conn, rw):
                if c is not None:
                    c.close()
            if host is not None:
                host.stop()

    def refresh(self, conn, decisions) -> bool:
        """Rebuild the views; True after a full render from the store."""
        t = self.clock()
        self.live = liveness(now=t).message()
        if conn is None:
            self.pools_view, self.agents_view = pool_lines([], int(t * 1000)), agent_lines([])
            return False
        status = store.schema_status(conn)
        if not status.readable:
            self.problem = status.message
            return False
        self.pools_view = pool_lines(queries.pools(conn, t), int(t * 1000))
        self.agents_view = agent_lines(queries.agents(conn, t), {d.pane: d for d in decisions or []})
        self.problem = ""
        return True


def loop(win, worker: Worker):
    curses.curs_set(0)
    curses.use_default_colors()
    for n, c in ((1, curses.COLOR_GREEN), (2, curses.COLOR_YELLOW), (3, curses.COLOR_RED),
                 (4, curses.COLOR_CYAN), (5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE), (6, -1)):
        curses.init_pair(n, c, -1)
    win.timeout(250)
    worker.start()
    mode = "collecting and nudging" if worker.watch else "view only (--watch to collect and nudge)"
    while True:
        status = f"usage-watch  {dt.datetime.now():%H:%M:%S}  {mode}  ·  r refresh  q quit"
        if worker.problem:
            status += f"  ·  ERROR {worker.problem}"
        draw(win, worker.pools_view, worker.agents_view, worker.events, status, worker.live)
        key = win.getch()
        if key in (ord("q"), ord("Q"), 27):
            worker.clean = True
            worker.stop.set()
            worker.wake.set()
            return
        if key in (ord("r"), ord("R")):
            worker.wake.set()


def run(scan_every: float = 5, watch: bool = False) -> None:
    if not sys.stdout.isatty():
        raise Problem("the dashboard needs an interactive terminal",
                      fix="run it in a terminal, or use `usage-watch status --json` from scripts")
    cfg = config.load()
    if watch and lock_held(default_lock_path()):
        raise Problem("another collector runtime is already running",
                      expected="one collector runtime per machine, holding run.lock",
                      fix="use `usage-watch dashboard` without --watch to view it, or stop the other one")
    worker = Worker(cfg, scan_every, watch)
    try:
        curses.wrapper(loop, worker)
    except KeyboardInterrupt:
        worker.clean = True
    finally:
        worker.stop.set()
        worker.wake.set()
        if worker.is_alive():
            worker.join(timeout=10)

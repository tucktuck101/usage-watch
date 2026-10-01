"""usage-watch command line. `usage-watch primer` explains the tool; --help lists commands."""

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import sys
import time
from importlib import resources
from pathlib import Path

from . import __version__, collectors, config, queries, screen, sh, store, watcher
from .adapters import ADAPTERS
from .errors import Problem
from .pool import Pools, now
from .runtime.liveness import Liveness, liveness
from .topology import scan

FAMILIES = {"omp": ["claude", "codex"], "claude": ["claude"], "codex": ["codex"]}


def out(obj, as_json: bool, text: str) -> None:
    print(json.dumps(obj, indent=2, default=str) if as_json else text)


def table(rows: list[list[str]], header: list[str]) -> str:
    rows = [header] + [[str(c) if c is not None else "-" for c in r] for r in rows]
    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)


# primer ---------------------------------------------------------------------

AGENT_SECTIONS = ("Guarantees", "Limits", "For agents")


def cmd_primer(a) -> int:
    text = resources.files("usage_watch").joinpath("primer.md").read_text()
    if a.agent:
        parts = re.split(r"(?m)^## ", text)
        keep = [p for p in parts[1:] if p.split("\n", 1)[0].strip() in AGENT_SECTIONS]
        text = ("# usage-watch, for agents\n\nusage-watch nudges AI agents in tmux that stopped on a usage "
                "limit, once their pool has refilled.\n\n" + "".join("## " + p for p in keep))
    print(text.rstrip())
    return 0


# map / status ---------------------------------------------------------------

def cmd_map(a) -> int:
    topo = scan()
    rows = [[p.id, p.where, p.harness.name, p.role, p.lane or "", p.project and os.path.basename(p.project),
             p.branch, p.workmux_status] for p in topo.agents]
    text = table(rows, ["PANE", "WHERE", "HARNESS", "ROLE", "LANE", "PROJECT", "BRANCH", "WORKMUX"])
    if not topo.agents:
        text = "no agent panes found in tmux (looked for: " + ", ".join(ad.name for ad in ADAPTERS) + ")"
    text += "".join(f"\nnote: {n}" for n in topo.notes)
    out({"agents": [p.as_dict() for p in topo.agents], "notes": topo.notes}, a.json, text)
    return 0


def read_store():
    """A read-only connection to the store, or a Problem saying how to start
    a collector. A store whose schema isn't this code's isn't read (D3)."""
    conn = store.connect(readonly=True)
    status = store.schema_status(conn)
    if not status.readable:
        conn.close()
        raise Problem(status.message,
                      "start `usage-watch run` so the collector migrates the store" if status.state == "older"
                      else "upgrade usage-watch")
    return conn


def liveness_dict(live: Liveness) -> dict:
    return {"state": live.state, "message": live.message(), "pid": live.pid,
            "heartbeat_age_s": live.heartbeat_age_s, "data_as_of": live.data_as_of}


def clock_text(ms: int | None, now_s: float) -> str:
    """A UTC-ms time as the reader's local clock: `14:02`, or `Fri 14:02` on another day."""
    if ms is None:
        return "-"
    at = dt.datetime.fromtimestamp(ms / 1000)
    return at.strftime("%H:%M") if at.date() == dt.datetime.fromtimestamp(now_s).date() else at.strftime("%a %H:%M")


def pool_rows(pools: list[dict], now_s: float) -> list[list]:
    return [[p["account_label"], p["window"],
             "-" if p["remaining_pct"] is None else f"{p['remaining_pct']:.0f}%",
             clock_text(p["resets_at"], now_s), p["source"],
             p["age"] + (" STALE" if p["stale"] else "")] for p in pools]


def cmd_status(a) -> int:
    t = time.time()
    live = liveness(now=t)
    conn = read_store()
    try:
        pools = queries.pools(conn, t)
        agents = queries.agents(conn, t)
    finally:
        conn.close()
    text = live.message() + "\n\nPOOLS\n"
    text += (table(pool_rows(pools, t), ["ACCOUNT", "WINDOW", "LEFT", "RESETS", "SOURCE", "AGE"])
             if pools else "no capacity readings yet")
    text += "\n\nAGENTS\n"
    text += (table([[g["pane"], g["harness"], g["state"], g["model"],
                     g["session_key"] and g["session_key"][:20], g["account_label"] or g["account_state"],
                     f"{g['age_s']:.0f}s"] for g in agents],
                   ["PANE", "HARNESS", "STATE", "MODEL", "SESSION", "ACCOUNT", "AGE"])
             if agents else "no agent panes seen in the last 2 minutes")
    out({"liveness": liveness_dict(live), "pools": pools, "agents": agents,
         "checked_at": now().isoformat()}, a.json, text)
    return 0


# usage ----------------------------------------------------------------------

LOOK_WHO, USAGE_VIEW = "cli", "usage"


def fmt(n: int) -> str:
    return f"{n:,}"


def render_usage(result: dict) -> str:
    rows = []
    for r in result["rows"] + [{"label": "TOTAL", **result["total"]}]:
        rows.append([r["label"], fmt(r["requests"]), fmt(r["total_input_tokens"]),
                     fmt(r["uncached_input_tokens"]), fmt(r["cache_read_input_tokens"]),
                     fmt(r["cache_write_input_tokens"]), fmt(r["output_tokens"]),
                     fmt(r["unknown_requests"]) if r["unknown_requests"] else ""])
    text = table(rows, [result["by"].upper(), "REQUESTS", "INPUT", "UNCACHED", "CACHE READ",
                        "CACHE WRITE", "OUTPUT", "UNKNOWN"])
    total = result["total"]
    if total["unknown_requests"]:
        text += (f"\n\n{fmt(total['unknown_requests'])} request(s) had a token field unknown "
                 "(UNKNOWN): the sums are of known values only, never counting an unknown as 0.")
    if result["shares"]:
        sh_ = result["shares"]
        if total["requests"]:
            text += "\n" + ", ".join(f"{k} {sh_[k] * 100:.1f}%" for k in ("attributed", "ambiguous", "unattributed"))
            text += " of requests"
    if not result["include_auxiliary"]:
        text += "\nfilter: auxiliary calls excluded (--no-auxiliary)"
    return text


def cmd_usage(a) -> int:
    t = time.time()
    now_ms = int(t * 1000)
    live = liveness(now=t)
    conn = read_store()
    last = None
    note = None
    try:
        if a.since_last:
            last = queries.since_last_look(conn, LOOK_WHO, USAGE_VIEW)
            note = last.note()
            if last.since_ms is None:
                since_ms = queries.parse_since("24h", t)
                note += "; showing the last 24h"
            else:
                since_ms = last.since_ms
        else:
            try:
                since_ms = queries.parse_since(a.since or "24h", t)
            except ValueError:
                raise Problem(f"cannot read --since {a.since!r}",
                              "give a span like 24h, 7d or 30m, or a date like 2026-10-01",
                              expected="a span or an ISO date or time") from None
        result = queries.usage(conn, since_ms, None, a.by, include_auxiliary=not a.no_auxiliary)
    finally:
        conn.close()
    head = [live.message(), f"usage since {clock_text(since_ms, t)} "
                            f"({dt.datetime.fromtimestamp(since_ms / 1000):%Y-%m-%d %H:%M}), by {a.by}"]
    if note:
        head.append(f"note: {note}")
    payload = {"liveness": liveness_dict(live), "since": dt.datetime.fromtimestamp(
        since_ms / 1000, dt.timezone.utc).isoformat(), "last_look": last.as_dict() if last else None,
        "note": note, **result}
    out(payload, a.json, "\n".join(head) + "\n\n" + render_usage(result))
    # D3: a one-shot look opens, is seen and closes at one moment. Piped or
    # --json output records nothing unless asked with --mark.
    if (a.since_last or a.mark) and (a.mark or (not a.json and sys.stdout.isatty())):
        rw = store.connect()
        try:
            queries.open_look(rw, LOOK_WHO, USAGE_VIEW, now_ms)
            queries.touch_look(rw, LOOK_WHO, USAGE_VIEW, now_ms)
            queries.close_look(rw, LOOK_WHO, USAGE_VIEW, now_ms)
        finally:
            rw.close()
    return 0


# doctor ---------------------------------------------------------------------

def sanitize(text: str) -> str:
    text = text.replace(os.path.expanduser("~"), "~")
    text = re.sub(r'"request_id":"[^"]+"', '"request_id":"req_REDACTED"', text)
    return re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "user@example.com", text)


ERROR_CHARS = re.compile(r"[^\w.,:\[\] ]")


def cmd_doctor(a) -> int:
    if a.capture:
        plain, _ = screen.capture(a.capture)
        print(sanitize(plain).rstrip())
        return 0
    checks, failed = [], False

    def check(name, ok, detail):
        nonlocal failed
        failed |= ok is False
        checks.append(("ok " if ok else ("-- " if ok is None else "!! ")) + f"{name}: {detail}")

    check("tmux", bool(sh.which("tmux")), sh.which("tmux") or "not found; install tmux")
    check("workmux", True if sh.which("workmux") else None,
          sh.which("workmux") or "not found (optional; lanes are inferred from git worktrees)")
    cfg = config.load()
    check("config", True if cfg.exists else None, f"{cfg.path}" if cfg.exists else f"none at {cfg.path}; defaults apply")
    live = liveness()
    check("collector", {"running": True, "stalled": False}.get(live.state), live.message())
    try:
        conn = read_store()
    except Problem as p:
        check("store", None, p.what)
    else:
        try:
            from .runtime.reconcile import orphan_counts
            orphans = orphan_counts(conn)
            rows = conn.execute("SELECT collector, last_run_at, last_success_at, last_new_at, last_error"
                                " FROM collector_status ORDER BY collector").fetchall()
        finally:
            conn.close()
        t = time.time()
        if not rows:
            check("collectors", None, "none has run yet")
        for name, run_at, ok_at, new_at, error in rows:
            detail = (f"last ran {clock_text(run_at, t)}, last succeeded {clock_text(ok_at, t)}, "
                      f"last new data {clock_text(new_at, t)}")
            if orphans.get(name):
                detail += f", {orphans[name]} unlinked observation(s)"
            if error:  # stored as `<Type> at <path>[.<field>]`: field names, never values (D5)
                detail += "; error: " + ERROR_CHARS.sub("", error)
            check(f"collector {name}", False if error else True, detail)
    for ad in ADAPTERS:
        checks.append(f"   adapter {ad.name}: {ad.verified}")
    print("\n".join(checks))
    print()
    cmd_map(argparse.Namespace(json=False))
    return 1 if failed else 0


# init -----------------------------------------------------------------------

def cmd_init(a) -> int:
    if a.claude_statusline:
        return cmd_init_claude_statusline(a)
    if a.undo or a.yes:
        raise Problem("--undo and --yes apply only to `init --claude-statusline`",
                      fix="rerun as `usage-watch init --claude-statusline --undo` (or --yes)")
    cfg = config.load()
    if cfg.exists and not a.force:
        raise Problem(f"a config file already exists at {cfg.path}",
                      fix="edit it directly, or rerun with --force to replace it")
    pools = Pools()
    given = {}
    for item in a.account or []:
        m = re.fullmatch(r"(\w+)\.(\w+)=(\S+)", item)
        if not m:
            raise Problem(f"cannot read --account {item!r}", expected="HARNESS.FAMILY=PROVIDER, e.g. omp.claude=claude@1a2b3c4d",
                          fix="rerun with the value in that form")
        given.setdefault(m[1], {})[m[2]] = m[3]
    providers = pools.providers()
    print("Accounts found:" if providers else "No accounts found yet (capacity sources arrive with plan item C1).")
    for pid, info in sorted(providers.items()):
        print(f"  {pid:24} {info.get('plan', ''):10} {info.get('displayName', '')}")
    accounts: dict = {}
    for harness, families in FAMILIES.items():
        for family in families:
            if family in given.get(harness, {}):
                accounts.setdefault(harness, {})[family] = given[harness][family]
                continue
            ids = sorted(p for p in providers if p == family or p.startswith(family + "@"))
            if len(ids) == 1:
                accounts.setdefault(harness, {})[family] = ids[0]
            elif len(ids) > 1:
                if not sys.stdin.isatty():
                    raise Problem(
                        f"{harness} could use any of {', '.join(ids)} for {family} models, and there is no terminal to ask",
                        fix=f"rerun with --account {harness}.{family}=<one of those ids>",
                    )
                choice = ""
                while choice not in ids:
                    choice = input(f"Which account does {harness} use for {family} models? [{'/'.join(ids)}] ").strip()
                accounts.setdefault(harness, {})[family] = choice
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    cfg.path.write_text(config.render(accounts))
    print(f"\nwrote {cfg.path}\nnext: `usage-watch doctor` to check what it sees, then `usage-watch run` in a tmux window")
    return 0


# init --claude-statusline ---------------------------------------------------

TAP = "usage-watch statusline-tap --"
_PLAIN = re.compile(r"[\w@%+=:,./~ -]+")


def claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def tap_prefix() -> str:
    """The tap, by absolute path where one can be found: Claude Code runs the
    status line with its own PATH, which may not include usage-watch's."""
    found = shutil.which("usage-watch")
    return f"{shlex.quote(found)} statusline-tap --" if found else TAP


def wrap_statusline_command(existing: str | None, prefix: str | None = None) -> str:
    """The tap in front of the user's command. A command of plain words is
    kept verbatim; anything with shell syntax is quoted into one argument,
    which the tap runs with /bin/sh -c, so it means what it meant before."""
    prefix = prefix or TAP
    if not existing or not existing.strip():
        return prefix
    if _PLAIN.fullmatch(existing.strip()):
        return f"{prefix} {existing.strip()}"
    return f"{prefix} {shlex.quote(existing)}"


def _write_atomic(path: Path, data: bytes, like: Path | None = None) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        if like is not None and like.exists():
            os.chmod(tmp, like.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def cmd_init_claude_statusline(a) -> int:
    settings = claude_settings_path()
    backup = settings.with_name(settings.name + ".usage-watch.bak")
    if a.undo:
        if not backup.exists():
            raise Problem(f"no backup at {backup}; nothing to undo",
                          expected="a backup written by `usage-watch init --claude-statusline`",
                          fix=f"check the statusLine entry in {settings} by hand")
        original = backup.read_bytes()
        if original:
            _write_atomic(settings, original, like=settings)
        else:  # there was no settings.json before
            settings.unlink(missing_ok=True)
        backup.unlink()
        print(f"restored {settings} from {backup}")
        return 0

    original = settings.read_bytes() if settings.exists() else b""
    try:
        data = json.loads(original) if original.strip() else {}
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise Problem(f"{settings} is not a JSON object",
                      expected="Claude Code's settings.json",
                      fix=f"fix {settings} by hand, then rerun")
    before = data.get("statusLine")
    existing = before.get("command") if isinstance(before, dict) else None
    if isinstance(existing, str) and "statusline-tap" in existing:
        raise Problem("the Claude status line already runs through usage-watch statusline-tap",
                      expected="a statusLine command that is not wrapped yet",
                      fix="nothing to do; `usage-watch init --claude-statusline --undo` removes it")
    if backup.exists():
        raise Problem(f"a backup already exists at {backup}",
                      expected="no earlier backup, so it is never overwritten",
                      fix=f"run `usage-watch init --claude-statusline --undo`, or move {backup} aside")
    after = dict(before) if isinstance(before, dict) else {}
    after["type"] = "command"
    after["command"] = wrap_statusline_command(existing if isinstance(existing, str) else None, tap_prefix())

    print(f"{settings}: statusLine")
    print(f"  before: {json.dumps(before) if before is not None else '(none)'}")
    print(f"  after:  {json.dumps(after)}")
    if not a.yes:
        if not sys.stdin.isatty():
            raise Problem("no terminal to confirm the change",
                          fix="rerun with --yes to apply it as shown")
        if input("Apply this change? [y/N] ").strip().lower() not in ("y", "yes"):
            print("not changed")
            return 1
    data["statusLine"] = after
    settings.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(backup, original, like=settings if settings.exists() else None)
    _write_atomic(settings, (json.dumps(data, indent=2) + "\n").encode(), like=settings)
    print(f"applied; the original is kept at {backup}\nundo: usage-watch init --claude-statusline --undo")
    return 0


# statusline-tap ---------------------------------------------------------------

def cmd_statusline_tap(argv: list[str]) -> int:
    from .collectors import statusline
    if argv and argv[0] == "--":
        argv = argv[1:]
    try:
        stdin = b"" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read()
    except Exception:
        stdin = b""
    code, stdout = statusline.tap(argv, stdin)
    sys.stdout.flush()
    sys.stdout.buffer.write(stdout)
    sys.stdout.buffer.flush()
    return code


# wait / nudge / run ---------------------------------------------------------

def cmd_wait(a) -> int:
    cfg = config.load()
    if not a.provider and not a.pane:
        raise Problem("wait needs --provider or --pane", fix="e.g. usage-watch wait --provider claude")
    deadline = time.monotonic() + a.timeout if a.timeout else None
    while True:
        pools = Pools()
        provider, model = a.provider, None
        if a.pane:
            obs = next((o for o in watcher.observe(scan(), cfg, pools) if o.pane.id == a.pane), None)
            if obs is None:
                raise Problem(f"no agent pane {a.pane}", fix="list agent panes with `usage-watch map`")
            model = obs.reading.model
            provider = pools.resolve(obs.pane.harness.name, obs.pane.harness.family(model), cfg.accounts)
        cap = pools.capacity(provider, a.min if a.min is not None else cfg.min_remaining, model)
        if cap.ok:
            print(f"{provider}: {cap.why}")
            return 0
        pause = 60.0
        if cap.resets_at:
            pause = max(15.0, min(300.0, (cap.resets_at - now()).total_seconds() + 30))
        if deadline and time.monotonic() + pause > deadline:
            raise Problem(f"{provider} still has no capacity: {cap.why}",
                          fix="wait longer (raise --timeout), or switch the work to a pool with capacity")
        time.sleep(pause)


def policy_class():
    """The nudge policy (F5), or None while it isn't installed."""
    try:
        from .policy import Policy
    except ImportError:
        return None
    return Policy


def alerts_class():
    """Pool alerts (A1), or None while they aren't installed."""
    try:
        from .alerts import Alerts
    except ImportError:
        return None
    return Alerts


def cmd_nudge(a) -> int:
    cfg = config.load()
    pane = next((p for p in scan().agents if p.id == a.pane), None)
    if pane is None:
        raise Problem(f"no agent pane {a.pane}", fix="list agent panes with `usage-watch map`")
    plain, styled = screen.capture(pane.id)
    reading = pane.harness.read(plain, styled)
    if reading.state == "typing":
        raise Problem(f"{a.pane} has text in its input box", fix="clear or send it first; usage-watch never types over it")
    if not a.force:
        if reading.state != "stalled":
            raise Problem(f"{a.pane} is {reading.state}: {reading.note or 'nothing to nudge'}",
                          expected="a stalled pane whose pool has capacity",
                          fix="wait for that, or rerun with --force if you are sure")
        Policy = policy_class()
        if Policy is None:
            raise Problem("the nudge policy isn't installed, so capacity can't be confirmed",
                          expected="the D8 nudge policy (plan item F5)",
                          fix="rerun with --force if you are sure")
        conn = read_store()
        try:
            decision = next((d for d in Policy(conn, cfg, act=False).tick() if d.pane == a.pane), None)
        finally:
            conn.close()
        if decision is None or decision.action != "nudge":
            action = decision.action if decision else "none"
            reason = (decision.reason if decision else "") or "nothing to do"
            raise Problem(f"{a.pane} is {reading.state}, action {action}: {reason}",
                          expected="a stalled pane whose pool has capacity, by the nudge policy (D8)",
                          fix="wait for that, or rerun with --force if you are sure")
    text = a.text or cfg.nudge_for(pane)
    screen.type_into(pane.id, text)
    print(f"typed {text!r} into {a.pane}")
    return 0


def cmd_dashboard(a) -> int:
    from . import dashboard
    dashboard.run(scan_every=a.scan, watch=a.watch)
    return 0


def log_decisions(log, decisions, waits: dict) -> None:
    """Log nudges and escalations; a wait only when its reason changes."""
    for d in decisions:
        label = f"{d.pane} {d.harness or ''}".rstrip()
        if d.action in ("nudge", "escalate"):
            log(f"{'ESCALATE ' if d.action == 'escalate' else ''}{label}: {d.action}; {d.reason}")
            waits.pop(d.pane, None)
        elif d.action == "wait":
            if waits.get(d.pane) != d.reason:
                waits[d.pane] = d.reason
                log(f"{label} stalled; waiting: {d.reason}")
        else:
            waits.pop(d.pane, None)


class Host:
    """The collector runtime with the nudge policy and pool alerts, in one
    process (D3): what `run` and `dashboard --watch` host. Ownership stays
    per part: the runtime writes data tables, the policy `nudges`, through
    its own read-write connection."""

    def __init__(self, cfg, act: bool, log, sources=None):
        from .runtime.core import Runtime
        self.cfg, self.act, self.log = cfg, act, log
        self.runtime = Runtime(collectors.default_sources() if sources is None else sources)
        self.policy = self.alerts = None
        self._conns: list = []
        self.waits: dict = {}

    def start(self) -> None:
        self.runtime.start()  # the lock, then migrations
        try:
            Policy, Alerts = policy_class(), alerts_class()
            if Policy is not None:
                conn = store.connect(self.runtime.db_path)
                self._conns.append(conn)
                self.policy = Policy(conn, self.cfg, act=self.act, notify=watcher.notify)
            else:
                self.log("the nudge policy isn't installed: collecting only")
            if Alerts is not None:
                conn = store.connect(self.runtime.db_path, readonly=True)
                self._conns.append(conn)
                self.alerts = Alerts(conn, notify=watcher.notify)
        except BaseException:
            self.stop()
            raise

    def interval(self) -> float:
        return min((float(s.interval_s) for s in self.runtime.pull), default=5.0)

    def tick(self) -> list:
        """One pass: collect, decide, alert. Returns the policy's decisions."""
        self.runtime.run_once()
        decisions = []
        if self.policy is not None:
            try:
                decisions = self.policy.tick()
            except Exception as e:  # D8 fails closed: no decision is no nudge
                self.log(f"ESCALATE nudge policy failed: {type(e).__name__}")
            log_decisions(self.log, decisions, self.waits)
        if self.alerts is not None:
            try:
                for msg in self.alerts.check():
                    self.log(f"ALERT {msg}")
            except Exception as e:
                self.log(f"alerts failed: {type(e).__name__}")
        return decisions

    def stop(self) -> None:
        for conn in self._conns:
            conn.close()
        self._conns = []
        self.runtime.stop()


def cmd_run(a) -> int:
    cfg = config.load()
    log = watcher.Log()
    host = Host(cfg, act=not a.no_nudge, log=log)
    host.start()
    try:
        every = float(a.interval) if a.interval else host.interval()
        log(f"collecting{'' if a.no_nudge else ' and nudging'}; config "
            f"{cfg.path if cfg.exists else '(none, defaults)'}; store {host.runtime.db_path}; every {every:g}s")
        while True:
            host.tick()
            if a.once:
                break
            time.sleep(every)
    except KeyboardInterrupt:
        log("stopped")
    finally:
        host.stop()
    return 0


# entry ----------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="usage-watch",
        description="Find AI coding agents in tmux stalled on a usage limit, and nudge them when their "
                    "pool refills. New here? Run `usage-watch primer`.",
    )
    p.add_argument("--version", action="version", version=f"usage-watch {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    s = sub.add_parser("primer", help="explain what this does and how it works")
    s.add_argument("--agent", action="store_true", help="only the guarantees, limits and agent contract")
    s.set_defaults(fn=cmd_primer)

    s = sub.add_parser("map", help="show every agent pane found, with harness, project and role")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_map)

    s = sub.add_parser("status", help="from the store: collector liveness, each pool, each agent")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("usage", help="token usage over a time range, broken down (V1); "
                                     "--since-last: since you last looked (V2)")
    when = s.add_mutually_exclusive_group()
    when.add_argument("--since", metavar="WHEN", help="24h, 7d, 30m, or an ISO date or time (default 24h)")
    when.add_argument("--since-last", action="store_true",
                      help="since this view was last closed; records this look")
    s.add_argument("--by", choices=queries.BY, default="harness", help="group by (default harness)")
    s.add_argument("--no-auxiliary", action="store_true",
                   help="leave out harness side calls (named filter; totals then exclude them)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--mark", action="store_true",
                   help="record this look even when output is piped or --json")
    s.set_defaults(fn=cmd_usage)

    s = sub.add_parser("doctor", help="check the setup, the collector and each source, or record a pane's screen")
    s.add_argument("--capture", metavar="PANE", help="print a pane's screen, with personal details removed")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("init", help="write a config file, choosing accounts where there are several; "
                                    "--claude-statusline installs the status line tap")
    s.add_argument("--account", action="append", metavar="HARNESS.FAMILY=PROVIDER")
    s.add_argument("--force", action="store_true", help="replace an existing config file")
    s.add_argument("--claude-statusline", action="store_true",
                   help="instead: wrap Claude Code's statusLine command in ~/.claude/settings.json with "
                        "`usage-watch statusline-tap --`, after showing the change")
    s.add_argument("--undo", action="store_true", help="with --claude-statusline: restore the backup")
    s.add_argument("--yes", action="store_true", help="with --claude-statusline: apply without asking")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("statusline-tap", help="run as Claude's status line: keep its rate_limits, "
                                              "then run your own command with the same input",
                       usage="usage-watch statusline-tap -- [YOUR STATUS LINE COMMAND ...]")
    s.add_argument("command", nargs=argparse.REMAINDER, help="your status line command, after --")
    s.set_defaults(fn=None)

    s = sub.add_parser("wait", help="block until a pool has capacity")
    s.add_argument("--provider", help="provider id, as `usage-watch doctor` lists it")
    s.add_argument("--pane", help="the pool this pane draws on")
    s.add_argument("--min", type=float, help="session %% required (default from config)")
    s.add_argument("--timeout", type=float, help="give up after this many seconds")
    s.set_defaults(fn=cmd_wait)

    s = sub.add_parser("nudge", help="nudge one pane now, with the same safety checks")
    s.add_argument("pane")
    s.add_argument("--text", help="type this instead of the configured nudge")
    s.add_argument("--force", action="store_true", help="nudge even if it is not stalled with capacity")
    s.set_defaults(fn=cmd_nudge)

    s = sub.add_parser("dashboard", help="live view of pools and agents from the store; "
                                         "--watch also collects and nudges")
    s.add_argument("--watch", action="store_true",
                   help="also host the collector and nudge policy, like `run` (one per machine)")
    s.add_argument("--scan", type=float, default=5, help="seconds between refreshes (default 5)")
    s.set_defaults(fn=cmd_dashboard)

    s = sub.add_parser("run", help="collect into the store and nudge until stopped; one per machine")
    s.add_argument("--once", action="store_true", help="one pass, then exit")
    s.add_argument("--no-nudge", action="store_true", help="collect only: decide, but never type")
    s.add_argument("--interval", type=float, metavar="N",
                   help="seconds between passes (default: the shortest collector interval)")
    s.set_defaults(fn=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    # Everything after `statusline-tap` is the user's command, passed through
    # untouched; argparse's handling of `--` and option-like words differs by
    # Python version, so it never sees them. Only a bare -h/--help reaches it.
    if argv and argv[0] == "statusline-tap" and argv[1:] not in (["-h"], ["--help"]):
        return cmd_statusline_tap(argv[1:])
    a = parser().parse_args(argv)
    try:
        return a.fn(a)
    except Problem as e:
        print(e.render(), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

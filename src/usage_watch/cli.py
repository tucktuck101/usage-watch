"""usage-watch command line. `usage-watch primer` explains the tool; --help lists commands."""

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import time
from importlib import resources
from pathlib import Path

from . import __version__, config, screen, sh, watcher
from .adapters import ADAPTERS
from .errors import Problem
from . import pool
from .pool import Pools, now
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


def cmd_status(a) -> int:
    cfg = config.load()
    pools = Pools()
    observations = watcher.observe(scan(), cfg, pools)
    for o in observations:  # show every agent's pool, not only stalled ones
        if o.capacity is None and o.reading.state != "unknown":
            try:
                o.provider = o.provider or pools.resolve(o.pane.harness.name, o.pane.harness.family(o.reading.model), cfg.accounts)
                o.capacity = pools.capacity(o.provider, cfg.min_remaining, o.reading.model)
            except Problem as p:
                o.reason = o.reason or p.what
    rows = [[o.pane.id, o.pane.harness.name, o.pane.role, o.reading.model, o.reading.state, o.provider,
             ("ok " if o.capacity.ok else "low ") + o.capacity.why if o.capacity else o.reason,
             o.action if o.action != "none" else ""] for o in observations]
    text = table(rows, ["PANE", "HARNESS", "ROLE", "MODEL", "STATE", "PROVIDER", "POOL", "ACTION"])
    escalations = [o.reason for o in observations if o.action == "escalate"]
    text += "".join(f"\n\n{r}" for r in escalations)
    out({"agents": [o.as_dict() for o in observations], "checked_at": now().isoformat()}, a.json, text)
    return 0


# doctor ---------------------------------------------------------------------

def sanitize(text: str) -> str:
    text = text.replace(os.path.expanduser("~"), "~")
    text = re.sub(r'"request_id":"[^"]+"', '"request_id":"req_REDACTED"', text)
    return re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "user@example.com", text)


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
    pools = Pools()
    try:
        ids = sorted(pools.providers())
        if not pool.SOURCES:
            check("pools", None, "no capacity source yet (plan item C1); stalled panes will wait, not be nudged")
            raise StopIteration
        check("pools", bool(ids), ", ".join(ids) or "no providers found")
        for e in pools.errors():
            check(f"pool {e.get('providerId')}", None, e.get("message", "error"))
        for harness, families in FAMILIES.items():
            for family in families:
                try:
                    check(f"account {harness}/{family}", True, pools.resolve(harness, family, cfg.accounts))
                except Problem as p:
                    check(f"account {harness}/{family}", None, p.what)
    except StopIteration:
        pass
    except Problem as p:
        check("pools", False, p.what)
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


def cmd_nudge(a) -> int:
    cfg = config.load()
    obs = next((o for o in watcher.observe(scan(), cfg, Pools()) if o.pane.id == a.pane), None)
    if obs is None:
        raise Problem(f"no agent pane {a.pane}", fix="list agent panes with `usage-watch map`")
    if obs.reading.state == "typing":
        raise Problem(f"{a.pane} has text in its input box", fix="clear or send it first; usage-watch never types over it")
    if obs.action != "nudge" and not a.force:
        raise Problem(
            f"{a.pane} is {obs.reading.state}, action {obs.action}: {obs.reason or 'nothing to do'}",
            expected="a stalled pane whose pool has capacity",
            fix="wait for that, or rerun with --force if you are sure",
        )
    if a.text:
        cfg.nudge, cfg.roles, cfg.overrides = a.text, {}, []
    print(watcher.nudge(obs, cfg, watcher.Memory()))
    return 0


def cmd_dashboard(a) -> int:
    from . import dashboard
    dashboard.run(scan_every=a.scan, pool_every=a.pools, watch=a.watch)
    return 0


def cmd_run(a) -> int:
    cfg = config.load()
    if a.interval:
        cfg.interval = a.interval
    watcher.run(cfg, once=a.once, dry_run=a.dry_run)
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

    s = sub.add_parser("status", help="map plus each pane's state and its pool's capacity")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("doctor", help="check the setup, or record a pane's screen")
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

    s = sub.add_parser("dashboard", help="live view of pools and agents; --watch also nudges")
    s.add_argument("--watch", action="store_true", help="also nudge, like `run` (one per machine)")
    s.add_argument("--scan", type=float, default=5, help="seconds between agent scans (default 5)")
    s.add_argument("--pools", type=float, default=60, help="seconds between pool reads (default 60)")
    s.set_defaults(fn=cmd_dashboard)

    s = sub.add_parser("run", help="watch and nudge until stopped; one per machine")
    s.add_argument("--once", action="store_true", help="one scan, then exit")
    s.add_argument("--dry-run", action="store_true", help="report nudges without typing")
    s.add_argument("--interval", type=int, help="seconds between scans")
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

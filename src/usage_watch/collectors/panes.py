"""The `panes` collector (D2 "Session to pane", prototype assumption A7).

A minimal piece of C4: the two authoritative file methods that join a
running session to its tmux pane.

- Claude: `~/.claude/sessions/<pid>.json`. A file whose `pid` is alive and
  whose `tmux` field (`session:@window.%pane`) parses gives the pane of
  session `claude:<sessionId>`. Method `claude_session_file`.
- omp: `~/.omp/agent/terminal-sessions/tmux-%N`. Line 1 is the cwd, line 2
  the session file's path; the session ID is that file's `type:"session"`
  header. Kept only while pane `%N` exists in tmux. Method
  `omp_terminal_session`.
- omp and Codex, lower priority (D2's last row): the pane's harness
  process, then the one session `.jsonl` it holds open under the
  harness's session root (`/proc/<pid>/fd` on Linux, `lsof -p <pid> -Fn`
  elsewhere). The session ID is omp's `type:"session"` header `id`, or
  Codex's first `session_meta` `payload.id`. Method `open_session_file`,
  confidence `observed`. Skipped for a pane that already got evidence from
  an authoritative method this pass.

Pane evidence is always `live`. The watermark is a JSON
object, `"<session_key>|<pane>" -> first_observed_at`, for the pairings
seen in the last pass: a pairing seen again keeps its `first_observed_at`
and advances `last_confirmed_at`, so its live window grows while it holds.

Confirmed against this machine, 2026-10-01 (key names and shapes only):
- Claude session files are named `<pid>.json` with matching `pid` (int) and
  `sessionId` (str); `tmux` (str, `name:@N.%N`) is absent on a session not
  started in tmux.
- omp `terminal-sessions` holds `tmux-%N` and `ttys<N>` files. Line 1 is an
  absolute path (the cwd, which may since be gone), line 2 an absolute
  `.jsonl` path (which may since be gone), then optional further lines.
  The session file's line 1 is `type:"title"`, line 2 `type:"session"` with
  a str `id`.

Only those fields are read. No credentials, no network.

Readings of the design that this module leaves open are marked "Reading:".
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable

from .. import sh, topology
from ..errors import Problem
from ..model import AttributionEvidence, session_key
from .omp import OmpSessionSource

__all__ = ["PanesSource", "default_claude_sessions", "default_codex_sessions",
           "default_omp_sessions", "default_omp_terminal_sessions", "harness_pid",
           "open_files", "pane_of"]

SOURCE = "panes"
CLAUDE_METHOD = "claude_session_file"
OMP_METHOD = "omp_terminal_session"
OPEN_FILE_METHOD = "open_session_file"
_OPEN_FILE_HARNESSES = ("omp", "codex")
PLATFORM = sys.platform  # module attribute so tests can stand in another one
PROC = Path("/proc")

# tmux session names can't contain ':' or '.', so the pane is unambiguous.
_TMUX_FIELD = re.compile(r"[^:]*:@\d+\.(%\d+)")
_OMP_FILE = re.compile(r"tmux-(%\d+)")


def default_claude_sessions() -> Path:
    return Path(os.path.expanduser("~/.claude/sessions"))


def default_omp_terminal_sessions() -> Path:
    return Path(os.path.expanduser("~/.omp/agent/terminal-sessions"))


def default_omp_sessions() -> Path:
    return Path(os.path.expanduser("~/.omp/agent/sessions"))


def default_codex_sessions() -> Path:
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "sessions"


def harness_pid(pane_pid: int, children) -> tuple[int, str] | None:
    """(pid, argv0) of the first omp or Codex process under a pane's shell,
    breadth-first over `topology.process_table()`'s children map."""
    queue, seen = [pane_pid], set()
    while queue:
        p = queue.pop(0)
        if p in seen:
            continue
        seen.add(p)
        for child, argv0, _args in children.get(p, []):
            if argv0 in _OPEN_FILE_HARNESSES:
                return child, argv0
            queue.append(child)
    return None


def open_files(pid: int) -> list[str]:
    """Paths a process has open: `/proc/<pid>/fd` on Linux, `lsof` elsewhere.
    Nothing when they can't be read."""
    if PLATFORM.startswith("linux"):
        try:
            fds = os.listdir(PROC / str(pid) / "fd")
        except OSError:
            return []
        paths = []
        for fd in fds:
            try:
                paths.append(os.readlink(PROC / str(pid) / "fd" / fd))
            except OSError:
                continue
        return paths
    r = sh.run(["lsof", "-p", str(pid), "-Fn"])
    # lsof can exit 1 over a warning and still list the files, so the
    # output is read whatever the code.
    return [line[1:] for line in r.out.splitlines() if line.startswith("n") and len(line) > 1]


def _under(path: str, root: Path) -> bool:
    try:
        return Path(os.path.realpath(path)).is_relative_to(os.path.realpath(root))
    except (OSError, ValueError):
        return False


def codex_session_id(path: str) -> str | None:
    """`payload.id` of a Codex rollout file's first `session_meta` line."""
    try:
        with open(path, "rb") as fh:
            for _ in range(8):
                line = fh.readline()
                if not line:
                    break
                if b'"session_meta"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    return None
                if not isinstance(rec, dict) or rec.get("type") != "session_meta":
                    continue
                payload = rec.get("payload")
                sid = payload.get("id") if isinstance(payload, dict) else None
                return sid if isinstance(sid, str) and sid else None
    except OSError:
        pass
    return None


def pane_of(tmux_field) -> str | None:
    """The `%N` pane ID in a Claude `tmux` field, or None if it doesn't parse."""
    if not isinstance(tmux_field, str):
        return None
    m = _TMUX_FIELD.fullmatch(tmux_field)
    return m.group(1) if m else None


def _alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # it exists, owned by someone else
    except (OSError, OverflowError):
        return False
    return True


def _load(watermark: str | None) -> dict[str, int]:
    if not watermark:
        return {}
    try:
        data = json.loads(watermark)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)}


class PanesSource:
    """A `PullSource` of session -> pane evidence. Never primary."""

    name = SOURCE
    primary = False
    merge = None

    def __init__(self, claude_sessions: str | os.PathLike | None = None,
                 omp_terminal_sessions: str | os.PathLike | None = None,
                 omp_sessions: str | os.PathLike | None = None,
                 codex_sessions: str | os.PathLike | None = None,
                 interval_s: float = 15, clock: Callable[[], float] = time.time):
        self.claude_sessions = (Path(claude_sessions) if claude_sessions is not None
                                else default_claude_sessions())
        self.omp_terminal_sessions = (Path(omp_terminal_sessions)
                                      if omp_terminal_sessions is not None
                                      else default_omp_terminal_sessions())
        self.omp_sessions = (Path(omp_sessions) if omp_sessions is not None
                             else default_omp_sessions())
        self.codex_sessions = (Path(codex_sessions) if codex_sessions is not None
                               else default_codex_sessions())
        self.interval_s = interval_s
        self.clock = clock
        self._tmux: list | None = None

    def collect(self, watermark: str | None) -> tuple[list, str | None]:
        prior = _load(watermark)
        now = int(self.clock() * 1000)
        seen: dict[str, int] = {}
        items: list = []
        authoritative_panes: set[str] = set()
        self._tmux = None  # tmux's panes, read at most once per pass

        def emit(skey: str, pane: str, method: str, confidence: str) -> None:
            key = f"{skey}|{pane}"
            if key in seen:
                return
            first = prior.get(key, now)
            seen[key] = first
            items.append(AttributionEvidence(
                subject_kind="session", subject_id=skey, dimension="pane", value=pane,
                method=method, source=SOURCE, confidence=confidence, validity="live",
                first_observed_at=first, last_confirmed_at=now,
            ))

        for skey, pane in self._claude():
            emit(skey, pane, CLAUDE_METHOD, "authoritative")
            authoritative_panes.add(pane)
        for skey, pane in self._omp():
            emit(skey, pane, OMP_METHOD, "authoritative")
            authoritative_panes.add(pane)
        for skey, pane in self._open_session_files(authoritative_panes):
            emit(skey, pane, OPEN_FILE_METHOD, "observed")

        # Reading: a pairing not seen this pass is dropped, so if it comes
        # back it starts a new window. The store keys evidence without
        # first_observed_at, so a returning pairing extends its old row.
        return items, json.dumps(seen, sort_keys=True)

    # -- Claude --

    def _claude(self):
        try:
            paths = sorted(self.claude_sessions.glob("*.json"))
        except OSError:
            return
        for path in paths:
            try:
                with open(path, "rb") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            sid = data.get("sessionId")
            pane = pane_of(data.get("tmux"))
            if not isinstance(sid, str) or not sid or pane is None:
                continue
            if not _alive(data.get("pid")):
                continue
            yield session_key("claude", sid), pane

    # -- omp --

    def _omp(self):
        try:
            names = sorted(os.listdir(self.omp_terminal_sessions))
        except OSError:
            return
        candidates = [(m.group(1), n) for n in names if (m := _OMP_FILE.fullmatch(n))]
        if not candidates:
            return
        live = self._live_panes()
        header = OmpSessionSource()  # only its header reader is used
        for pane, name in candidates:
            if pane not in live:
                continue
            try:
                with open(self.omp_terminal_sessions / name, encoding="utf-8",
                          errors="replace") as fh:
                    fh.readline()  # line 1: the cwd, not needed
                    session_path = fh.readline().rstrip("\r\n")
            except OSError:
                continue
            if not session_path:
                continue
            sid = header._session_id_of(session_path, {})
            if sid:
                yield session_key("omp", sid), pane

    def _panes(self) -> list:
        """tmux's panes now, read once per pass. No tmux, or no server: none."""
        if self._tmux is None:
            try:
                self._tmux = topology.tmux_panes()
            except Problem:
                self._tmux = []
        return self._tmux

    def _live_panes(self) -> set[str]:
        return {p.id for p in self._panes()}

    # -- omp and Codex: the harness process's open session file --

    def _open_session_files(self, skip: set[str]):
        # Reading: the pane list is tmux's, and the harness is found with
        # topology's process table, rather than a full `topology.scan()`,
        # whose git and workmux calls this method doesn't need.
        todo = [p for p in self._panes() if p.id not in skip]
        if not todo:
            return
        children = topology.process_table()
        header = OmpSessionSource()  # only its header reader is used
        for pane in todo:
            found = harness_pid(pane.pid, children)
            if found is None:
                continue
            pid, harness = found
            root = self.omp_sessions if harness == "omp" else self.codex_sessions
            hits = sorted({f for f in open_files(pid) if f.endswith(".jsonl") and _under(f, root)})
            # Reading: R6 saw exactly one; more than one is ambiguous, so none.
            if len(hits) != 1:
                continue
            if harness == "omp":
                sid = header._session_id_of(hits[0], {})
            else:
                sid = codex_session_id(hits[0])
            if sid:
                yield session_key(harness, sid), pane.id

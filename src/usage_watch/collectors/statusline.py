"""The Claude status line tap and its source (D6, "The Claude status line tap").

`usage-watch statusline-tap -- <the user's command...>` sits in front of the
user's own `statusLine` command. Claude Code hands the status line a JSON
object on stdin; when it carries `rate_limits` (R9), the tap keeps a snapshot
of them, then runs the user's command with the same stdin and passes its
output and exit code through unchanged. Nothing the tap does can break the
status line: a failed snapshot is dropped silently.

The snapshot, at `$XDG_STATE_HOME/usage-watch/claude-statusline/<session_id>.json`,
holds only `session_id`, `rate_limits` and `observed_at` (UTC ms). Within
`rate_limits`, only each window's `used_percentage` and `resets_at` are kept
(D5: ingestion is an allowlist). The rest of stdin is never written.

`ClaudeStatuslineSource` is the pull source (`claude.statusline`) that reads
those snapshots into capacity samples (D4, table 3), each with `account`
evidence from `~/.claude.json` `oauthAccount` (only `accountUuid` and
`organizationUuid` are read from it).

Reading: a single command argument that is not an executable on PATH is a
shell string (`init --claude-statusline` quotes a compound command into one
argument) and is run with `/bin/sh -c`, as Claude Code runs the status line.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .. import identity, model
from ..config import state_dir
from ..runtime.core import account_alias_value

NAME = "claude.statusline"
SNAPSHOT_DIR = "claude-statusline"
DEFAULT_INTERVAL_S = 10.0

# rate_limits key -> (D1 window, window_seconds)
WINDOWS: dict[str, tuple[str, int | None]] = {
    "five_hour": ("session", 18000),
    "seven_day": ("weekly", 604800),
    "spend_limit": ("other:spend_limit", None),
}
_KEPT_FIELDS = ("used_percentage", "resets_at")
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def snapshot_dir() -> Path:
    return state_dir() / SNAPSHOT_DIR


def _number(v) -> float | int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v if math.isfinite(v) else None


def _allowlisted(rate_limits: dict) -> dict:
    """Only the known windows, and only their used_percentage and resets_at."""
    kept = {}
    for key in WINDOWS:
        window = rate_limits.get(key)
        if isinstance(window, dict):
            fields = {f: window[f] for f in _KEPT_FIELDS if _number(window.get(f)) is not None}
            if fields:
                kept[key] = fields
    return kept


# --- The tap ---------------------------------------------------------------------------------

def write_snapshot(stdin_bytes: bytes, now_ms: int | None = None) -> Path | None:
    """Write the snapshot for this stdin, or return None when there is
    nothing to keep. Raises on a write failure; `tap` swallows it."""
    data = json.loads(stdin_bytes)
    if not isinstance(data, dict):
        return None
    session_id = data.get("session_id")
    rate_limits = data.get("rate_limits")
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        return None
    if not isinstance(rate_limits, dict):
        return None
    kept = _allowlisted(rate_limits)
    if not kept:
        return None
    snap = {"session_id": session_id, "rate_limits": kept,
            "observed_at": now_ms if now_ms is not None else int(time.time() * 1000)}
    directory = snapshot_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.json"
    tmp = directory / f".{session_id}.{os.getpid()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(snap, fh)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def _command(argv: list[str]) -> list[str]:
    if len(argv) == 1 and not shutil.which(argv[0]):
        return ["/bin/sh", "-c", argv[0]]
    return list(argv)


def tap(argv: list[str], stdin_bytes: bytes) -> tuple[int, bytes]:
    """Snapshot `rate_limits` if present, then run `argv` with the same stdin.
    Returns the command's exit code and stdout, unchanged. Never raises."""
    try:
        write_snapshot(stdin_bytes)
    except Exception:
        pass  # the status line must never break
    if not argv:
        return 0, b""
    try:
        r = subprocess.run(_command(argv), input=stdin_bytes, stdout=subprocess.PIPE)
    except OSError:
        return 127, b""  # as a shell reports a command it cannot run
    code = r.returncode if r.returncode >= 0 else 128 - r.returncode
    return code, r.stdout


# --- The source ------------------------------------------------------------------------------

def _claude_account_raw(home: Path) -> str | None:
    """`accountUuid|organizationUuid` from `~/.claude.json`, lowercased, or None.
    Nothing else in the file is kept."""
    try:
        data = json.loads((home / ".claude.json").read_bytes())
    except (OSError, ValueError):
        return None
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    acct, org = oauth.get("accountUuid"), oauth.get("organizationUuid")
    if not (isinstance(acct, str) and acct and isinstance(org, str) and org):
        return None
    return f"{acct}|{org}".lower()


def _read_snapshot(path: Path) -> dict | None:
    try:
        snap = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(snap, dict):
        return None
    sid, limits, at = snap.get("session_id"), snap.get("rate_limits"), snap.get("observed_at")
    if not (isinstance(sid, str) and _SESSION_ID.fullmatch(sid) and isinstance(limits, dict)
            and isinstance(at, int) and not isinstance(at, bool)):
        return None
    return snap


class ClaudeStatuslineSource:
    """PullSource for the tap's snapshots. Watermark: JSON map of snapshot
    file name -> the last `observed_at` emitted from it."""

    name = NAME
    primary = False
    merge = None

    def __init__(self, interval_s: float = DEFAULT_INTERVAL_S, secret: bytes | None = None,
                 home: Path | str | None = None, directory: Path | str | None = None):
        self.interval_s = interval_s
        self._secret = secret  # None: the per-install secret (D5)
        self._home = Path(home) if home is not None else None
        self._dir = Path(directory) if directory is not None else None

    def collect(self, watermark: str | None) -> tuple[list, str | None]:
        try:
            seen = json.loads(watermark) if watermark else {}
        except ValueError:
            seen = {}
        if not isinstance(seen, dict):
            seen = {}
        directory = self._dir if self._dir is not None else snapshot_dir()
        try:
            files = sorted(p for p in directory.iterdir() if p.suffix == ".json" and not p.name.startswith("."))
        except OSError:
            return [], watermark
        samples: list[model.CapacitySample] = []
        for path in files:
            snap = _read_snapshot(path)
            if snap is None:
                continue
            at = snap["observed_at"]
            last = seen.get(path.name)
            if isinstance(last, int) and at <= last:
                continue
            seen[path.name] = at
            samples.extend(_samples(snap))
        if not samples:
            return [], json.dumps(seen, sort_keys=True) if seen else watermark
        items: list = list(samples)
        raw = _claude_account_raw(self._home if self._home is not None else Path.home())
        if raw is not None:
            value = account_alias_value(
                "anthropic", "anthropic.account_org", identity.account("anthropic", raw, self._secret))
            items.extend(_evidence(s, value) for s in samples)
        return items, json.dumps(seen, sort_keys=True)


def _samples(snap: dict) -> list[model.CapacitySample]:
    out = []
    for key, (window, seconds) in WINDOWS.items():
        w = snap["rate_limits"].get(key)
        if not isinstance(w, dict):
            continue
        used = _number(w.get("used_percentage"))
        resets = _number(w.get("resets_at"))
        out.append(model.CapacitySample(
            source=NAME, stream_key="claude:" + snap["session_id"], window=window,
            confidence="authoritative", observed_at=snap["observed_at"], status="unknown",
            used_pct=float(used) if used is not None else None, window_seconds=seconds,
            resets_at=int(resets * 1000) if resets is not None else None,
        ))
    return out


def _evidence(s: model.CapacitySample, value: str) -> model.AttributionEvidence:
    return model.AttributionEvidence(
        subject_kind="capacity_sample",
        subject_id=f"{s.source}|{s.stream_key}|{s.window}|{s.observed_at}",
        dimension="account", value=value, method="state_file", source=NAME,
        confidence="inferred", validity="live",
        first_observed_at=s.observed_at, last_confirmed_at=s.observed_at,
    )


__all__: list[str] = ["ClaudeStatuslineSource", "NAME", "WINDOWS", "snapshot_dir", "tap",
                      "write_snapshot"]

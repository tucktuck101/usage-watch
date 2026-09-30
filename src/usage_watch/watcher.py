"""Observe every agent pane, decide, and nudge the stalled ones.

A pane is nudged only when all of these hold:
  - its adapter reads it as `stalled` (never `unknown`, `typing` or `resuming`)
  - it is not ignored in config, and it is not a lane that already handed off
  - the pool it draws on has capacity, per openusage
  - this stall has not been nudged already
"""

import datetime as dt
import fcntl
import json
import os
import sys
import time
from dataclasses import dataclass, field

from . import screen, sh
from .adapters import Reading
from .config import Config, state_dir
from .errors import Problem
from .pool import Capacity, Pools
from .topology import Pane, Topology, scan


@dataclass
class Observation:
    pane: Pane
    reading: Reading
    provider: str | None = None
    capacity: Capacity | None = None
    action: str = "none"      # none, nudge, wait, escalate
    reason: str = ""

    def as_dict(self) -> dict:
        d = self.pane.as_dict()
        d.update({
            "state": self.reading.state, "model": self.reading.model,
            "reset_hint": self.reading.reset_hint.isoformat() if self.reading.reset_hint else None,
            "provider": self.provider,
            "capacity": self.capacity.as_dict() if self.capacity else None,
            "action": self.action, "reason": self.reason,
        })
        return d


@dataclass
class Memory:
    nudged: dict = field(default_factory=dict)    # pane id -> error key
    strikes: dict = field(default_factory=dict)   # pane id -> nudges that did not hold
    escalated: set = field(default_factory=set)


def observe(topo: Topology, cfg: Config, pools: Pools, memory: Memory | None = None) -> list[Observation]:
    memory = memory or Memory()
    out = []
    for pane in topo.agents:
        try:
            plain, styled = screen.capture(pane.id)
            reading = pane.harness.read(plain, styled)
        except Problem as e:
            out.append(Observation(pane, Reading("unknown", note=e.what), action="none", reason=e.what))
            continue
        obs = Observation(pane, reading)
        out.append(obs)
        if reading.state != "stalled":
            if reading.state == "busy":
                memory.strikes.pop(pane.id, None)
                memory.escalated.discard(pane.id)
            obs.reason = reading.note
            continue
        if cfg.ignored(pane):
            obs.reason = "ignored by config"
            continue
        if pane.lane_done:
            obs.reason = "lane has written its handoff; nothing left to continue"
            continue
        try:
            obs.provider = pools.resolve(pane.harness.name, pane.harness.family(reading.model), cfg.accounts)
            obs.capacity = pools.capacity(obs.provider, cfg.min_remaining, reading.model)
        except Problem as e:
            obs.action, obs.reason = "escalate", e.render()
            continue
        if not obs.capacity.ok:
            obs.action, obs.reason = "wait", obs.capacity.why
            continue
        if memory.nudged.get(pane.id) == reading.error_key or memory.strikes.get(pane.id, 0) >= cfg.max_strikes:
            obs.action = "escalate"
            obs.reason = (f"{pane.id} ({pane.harness.name}, {pane.where}) is still stalled after a nudge "
                          f"while {obs.provider} shows {obs.capacity.why}")
            continue
        obs.action, obs.reason = "nudge", obs.capacity.why
    return out


def nudge(obs: Observation, cfg: Config, memory: Memory, dry_run: bool = False, settle: float = 12) -> str:
    text = cfg.nudge_for(obs.pane)
    if dry_run:
        return f"would type {text!r}"
    screen.type_into(obs.pane.id, text)
    memory.nudged[obs.pane.id] = obs.reading.error_key
    time.sleep(settle)
    plain, styled = screen.capture(obs.pane.id)
    after = obs.pane.harness.read(plain, styled).state
    if after == "stalled":
        memory.strikes[obs.pane.id] = memory.strikes.get(obs.pane.id, 0) + 1
    else:
        memory.strikes.pop(obs.pane.id, None)
    return f"typed {text!r}; pane now {after}"


def next_sleep(observations: list[Observation], interval: int) -> float:
    """Wake early when a waiting pool is due to reset before the next scan."""
    soonest = interval
    now = dt.datetime.now(dt.timezone.utc)
    for o in observations:
        if o.action == "wait":
            for t in (o.capacity.resets_at if o.capacity else None, o.reading.reset_hint):
                if t:
                    soonest = min(soonest, max(30, (t - now).total_seconds() + 30))
    return soonest


class Log:
    def __init__(self, to_file: bool = True):
        self.path = state_dir() / "usage-watch.log" if to_file else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg: str) -> None:
        line = f"{dt.datetime.now().astimezone():%Y-%m-%d %H:%M:%S} {msg}"
        print(line, flush=True)
        if self.path:
            with open(self.path, "a") as f:
                f.write(line + "\n")


def notify(title: str, msg: str) -> None:
    if sys.platform == "darwin":
        sh.run(["osascript", "-e", f"display notification {json.dumps(msg)} with title {json.dumps(title)}"])
    elif sh.which("notify-send"):
        sh.run(["notify-send", title, msg])


def acquire_lock():
    path = state_dir() / "run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.seek(0)
        holder = f.read().strip() or "unknown"
        raise Problem(
            f"another `usage-watch run` is already running (pid {holder})",
            expected="one watcher per machine, so no pane is nudged twice",
            fix=f"use the running one, or stop it first: kill {holder}",
        ) from None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


def run(cfg: Config, once: bool = False, dry_run: bool = False) -> None:
    lock = acquire_lock()  # held for the life of the process
    log = Log(to_file=not dry_run)
    memory = Memory()
    log(f"watching; config {cfg.path if cfg.exists else '(none, defaults)'}; interval {cfg.interval}s")
    while True:
        pools = Pools()
        try:
            topo = scan()
            observations = observe(topo, cfg, pools, memory)
        except Problem as e:
            log(f"ESCALATE {e.render()}")
            notify("usage-watch", e.what)
            observations = []
        for o in observations:
            label = f"{o.pane.id} {o.pane.harness.name} {o.pane.role} {o.pane.where}"
            if o.action == "nudge":
                log(f"{label} stalled; {o.reason}; {nudge(o, cfg, memory, dry_run)}")
            elif o.action == "wait":
                log(f"{label} stalled; waiting: {o.reason}")
            elif o.action == "escalate" and o.pane.id not in memory.escalated:
                memory.escalated.add(o.pane.id)
                log(f"ESCALATE {o.reason}")
                notify("usage-watch", o.reason.splitlines()[0])
        if once:
            break
        time.sleep(next_sleep(observations, cfg.interval))
    lock.close()

# usage-watch

AI coding agents stop when a subscription's usage limit runs out, and most of
them stay stopped after it resets, until someone types. usage-watch finds
those agents in tmux, waits until [OpenUsage](https://github.com/robinebers/openusage)
shows their pool has refilled, and types `continue` for you.

It works across every tmux session and every project at once. It finds
Claude Code, Codex and omp panes by their process tree, and learns your
project, worktree and workmux lane layout on every scan. You don't configure
it per project.

```
$ usage-watch status
PANE   HARNESS  ROLE          MODEL     STATE    PROVIDER         POOL                                 ACTION
%625   omp      orchestrator  Sonnet 5  stalled  claude@team      ok session 92% left, weekly 81% left  nudge
%1119  omp      lane          Sonnet 5  busy     claude@team      ok session 92% left, weekly 81% left
%621   claude   orchestrator  Opus 5.5  busy     claude           ok session 54% left, weekly 79% left
```

## Install

It needs Python 3.11 or later, tmux, and the OpenUsage app with its
`openusage` command. [workmux](https://workmux.dev) is optional: with it,
lanes are named and a finished lane is never nudged.

```sh
uv tool install git+https://github.com/tucktuck101/usage-watch
# or: pipx install git+https://github.com/tucktuck101/usage-watch
```

## Start

```sh
usage-watch primer    # what it does and how it works
usage-watch init      # pick accounts where you have more than one of a kind
usage-watch doctor    # check the setup and see what it finds
tmux new-window -d -n usage-watch 'usage-watch run'
```

`init` only asks something when OpenUsage reports two accounts of the same
family, for example a personal and a team Claude plan. The screen shows the
model, not the account, so it has to be told which harness uses which.

## Commands

| Command | What it does |
|---|---|
| `primer [--agent]` | Explains the tool. `--agent` prints only the guarantees, limits and agent contract, which you can paste into an `AGENTS.md` |
| `map [--json]` | Every agent pane found, with harness, project, branch, role and workmux lane |
| `status [--json]` | The map, plus each pane's state and its pool's capacity |
| `doctor [--capture PANE]` | Checks the setup. `--capture` prints a pane's screen with personal details removed, for recording an adapter fixture |
| `init [--account H.F=ID] [--force]` | Writes `~/.config/usage-watch/config.toml` |
| `wait --provider ID \| --pane PANE [--timeout S]` | Blocks until the pool has capacity. Exits 0, or 1 on timeout |
| `nudge PANE [--text T] [--force]` | Nudges one pane now, with the same safety checks |
| `run [--once] [--dry-run]` | Watches and nudges until stopped. One per machine |

Every command exits 0 on success and 1 on failure. Errors say what failed,
what was expected, and the fix.

## Safety

It types into a pane only when all of these hold: the pane's adapter reads it
as stalled on a usage limit, its input box is empty, OpenUsage confirms its
pool has capacity, and that stall hasn't been nudged already. If a pane stays
stuck, it escalates to you (a log line and a desktop notification) rather
than retrying. When unsure it does nothing, because a missed nudge only costs
a wait. `usage-watch primer` lists these guarantees in full.

## Harness support

| Harness | Limit message | Busy, idle and typing | Evidence |
|---|---|---|---|
| omp | verified | verified | live stalled, busy and idle panes |
| Claude Code | verified wording | verified | 2.1.285 binary strings, live busy pane; no live stall recorded yet |
| Codex | verified wording | **unverified** | real session log, 0.154.0 binary; stalls only when the screen is unambiguous |

To add or correct a harness, capture a real screen with
`usage-watch doctor --capture PANE`, save it under `tests/fixtures/`, and
write or adjust the module in `src/usage_watch/adapters/`. Each adapter
answers one question, which state this screen is in, and is tested against
recorded screens.

## Configuration

Everything is optional except choosing between two accounts of one family.

```toml
[defaults]
nudge = "continue"
interval = 300        # seconds between scans
min_remaining = 5     # session % needed before nudging
max_strikes = 3       # failed nudges before escalating

[accounts.omp]        # openusage provider per harness and model family
claude = "claude@1a2b3c4d"

[roles.orchestrator]  # nudge text per role: lane, orchestrator, standalone
nudge = "Usage limit cleared. Continue; if context was lost, re-read your runbook."

[[override]]          # per pane: match on pane, title or cwd_under
title = "scratch experiment"
ignore = true
```

## Develop

```sh
uv sync
uv run pytest
```

## Licence

0BSD. Use it for anything, no attribution required.

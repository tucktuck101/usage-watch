# usage-watch

AI coding agents stop when a subscription's usage limit runs out, and most of
them stay stopped after it resets, until someone types. usage-watch finds
those agents in tmux, waits until their pool has refilled, and types
`continue` for you.

> **Current state.** `usage-watch run` collects into a local SQLite store:
> agent states from tmux, token usage from each harness's session log, and
> pool capacity from credential-free sources only (Claude Code's status line,
> once you install the tap; Claude's own cached utilization; Codex's
> `rate_limits` in its session files; omp's usage cache). Nothing reads a
> token, key or credential file. A stalled pane is nudged only under the
> nudge policy in [D8](docs/design/D8-nudge-policy.md): a fresh reading for
> a known account shows its blocking window clear, and no other window is
> known to block. Otherwise it waits, and says why. Assumptions still to be
> confirmed are in [docs/prototype-assumptions.md](docs/prototype-assumptions.md).

It works across every tmux session and every project at once. It finds
Claude Code, Codex and omp panes by their process tree, and learns your
project, worktree and workmux lane layout on every scan. You don't configure
it per project.

```
$ usage-watch status
collector running, data as of 14:02:31

POOLS
ACCOUNT                       WINDOW   LEFT  RESETS  SOURCE                     AGE
anthropic account 88ffee      session  58%   16:10   claude.statusline          1m
anthropic account 88ffee      weekly   90%   Fri 09:00  claude.cached_utilization  2h00m STALE
unattributed (codex.rollout)  weekly   4%    -       codex.rollout              5m

AGENTS
PANE  HARNESS  STATE    MODEL     SESSION    ACCOUNT                   AGE
%1    claude   stalled  Opus 5.5  claude:s1  anthropic account 88ffee  4s
```

## Install

It needs Python 3.11 or later and tmux, and runs on macOS and Linux.
[workmux](https://workmux.dev) is optional: with it,
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
tmux new-window -d -n usage-watch 'usage-watch run'   # or: run --no-nudge, to collect only
```

`run` is the collector: everything else reads what it stores. The first run
backfills the session logs already on disk, which can take a few minutes.
Then `usage-watch status` shows pools and agents, and `usage-watch usage`
shows where tokens went.

Or watch it happen: `usage-watch dashboard --watch` collects, nudges and
shows each pool's limits and reset countdowns above every agent's state,
refreshing as it goes. Plain `usage-watch dashboard` only views, alongside a
`run` started elsewhere.

For fresher Claude capacity, `usage-watch init --claude-statusline` wraps
your Claude Code status line command so it also records the rate limits
Claude Code hands it. It shows the exact change first, applies it only when
you confirm, keeps a backup, and `--undo` restores it. Without it, Claude
capacity is only as fresh as the last time Claude Code refreshed its own
cached utilization.

`init` only asks something when it finds two accounts of the same family, for example a personal and a team Claude plan. The screen shows the
model, not the account, so it has to be told which harness uses which.

## Commands

| Command | What it does |
|---|---|
| `primer [--agent]` | Explains the tool. `--agent` prints only the guarantees, limits and agent contract, which you can paste into an `AGENTS.md` |
| `map [--json]` | Every agent pane found, with harness, project, branch, role and workmux lane |
| `status [--json]` | From the store: whether a collector is running and how old its data is, the newest reading of each pool (with its age, marked STALE past its source's display age), and each agent pane seen in the last 2 minutes with its session and account |
| `usage [--since 24h\|7d\|ISO \| --since-last] [--by harness\|provider\|model\|account\|project\|branch\|session\|day] [--no-auxiliary] [--json] [--mark]` | Token usage over a time range, broken down. Counts are known sums: requests with a field unknown are counted in UNKNOWN, never as 0. By account, project or branch it shows the attributed, ambiguous and unattributed shares, and every group adds up to the total. `--since-last` starts where you last closed this view, and records this look; piped or `--json` output records nothing unless `--mark` |
| `doctor [--capture PANE]` | Checks the setup, whether a collector is running, and each collector's last run, last new data and last error (field names only). `--capture` prints a pane's screen with personal details removed, for recording an adapter fixture |
| `init [--account H.F=ID] [--force]` | Writes `~/.config/usage-watch/config.toml` |
| `init --claude-statusline [--yes] [--undo]` | Wraps Claude Code's status line with `usage-watch statusline-tap`, after showing the change; `--undo` restores the backup |
| `wait --provider ID \| --pane PANE [--timeout S]` | Blocks until the pool has capacity. Exits 0, or 1 on timeout |
| `nudge PANE [--text T] [--force]` | Nudges one pane now: it must be stalled, its input empty, and the nudge policy must say `nudge` |
| `dashboard [--watch] [--scan S]` | Live terminal view of every pool (bars, reset countdowns, age) and every agent's state with the policy's decision. `--watch` also collects and nudges, in place of `run` |
| `run [--once] [--no-nudge] [--interval N]` | Collects into the store and nudges until stopped (Ctrl-C), without a screen. `--no-nudge` collects only. One per machine |

Every command exits 0 on success and 1 on failure. Errors say what failed,
what was expected, and the fix.

## Safety

It types into a pane only when all of these hold: the pane's adapter reads it
as stalled on a usage limit, its input box is empty, the pane's account is
known, a fresh reading (or the limit's own reset) shows the blocking window
clear and no other window is known to block, and that stall hasn't been
nudged already ([D8](docs/design/D8-nudge-policy.md)). If a pane stays
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
interval = 300        # unused by `run`, which passes at its collectors' pace (`run --interval N` to change)
min_remaining = 5     # session % `wait` needs; the nudge policy uses D8's min_remaining_pct (5)
max_strikes = 3       # failed nudges before escalating

[accounts.omp]        # provider id per harness and model family
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

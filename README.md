# usage-watch

A terminal observability tool for AI coding agents (Claude Code, Codex, omp)
running in tmux. It shows where your tokens went, how much subscription
capacity each account has left and when it resets, and what state every
agent is in, including which ones are stalled on a usage limit and when the
provider says they can retry.

It only watches. **usage-watch never types into a terminal.** It reads no
token, key or credential file, and stores no prompts.

> **Nudging was removed.** Earlier versions typed `continue` into agents
> stalled on a usage limit once their pool refilled. That was taken out of
> the code on 2026-10-01 and may return later; its specification stays in
> [design D8](docs/design/D8-nudge-policy.md). Assumptions still to be
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

## What it shows

- **Token usage history**, read from each harness's session log, with
  backfill of what is already on disk (`usage`).
- **Subscription capacity**, from credential-free sources only: Claude
  Code's status line (once you install the tap), Claude's own cached
  utilization, Codex's `rate_limits` in its session files, and omp's usage
  cache. Every reading carries its source and age (`status`, `dashboard`).
- **Agent state** for every pane: busy, idle, typing, resuming, stalled on a
  usage limit, or unknown. A stalled pane shows the retry time the provider
  itself gave, where it gave one.
- **Pool alerts** when a pool falls below 20% and 5%.
- **Setup checks** (`doctor`).

## Install

It needs Python 3.11 or later and tmux, and runs on macOS and Linux.
[workmux](https://workmux.dev) is optional: with it, lanes are named.

```sh
uv tool install git+https://github.com/tucktuck101/usage-watch
# or: pipx install git+https://github.com/tucktuck101/usage-watch
```

## Start

```sh
usage-watch primer    # what it does and how it works
usage-watch doctor    # check the setup and see what it finds
tmux new-window -d -n usage-watch 'usage-watch run'
```

`run` is the collector: everything else reads what it stores. The first run
backfills the session logs already on disk, which can take a few minutes.
Then `usage-watch status` shows pools and agents, and `usage-watch usage`
shows where tokens went.

Or watch it live: `usage-watch dashboard --watch` collects and shows each
pool's limits and reset countdowns above every agent's state, refreshing as
it goes. Plain `usage-watch dashboard` only views, alongside a `run` started
elsewhere.

For fresher Claude capacity, `usage-watch init --claude-statusline` wraps
your Claude Code status line command so it also records the rate limits
Claude Code hands it. It shows the exact change first, applies it only when
you confirm, keeps a backup, and `--undo` restores it. Without it, Claude
capacity is only as fresh as the last time Claude Code refreshed its own
cached utilization.

## Commands

| Command | What it does |
|---|---|
| `primer [--agent]` | Explains the tool. `--agent` prints only the guarantees, limits and agent contract, which you can paste into an `AGENTS.md` |
| `status [--json]` | From the store: whether a collector is running and how old its data is, the newest reading of each pool (with its age, marked STALE past its source's display age), and each agent pane seen in the last 2 minutes with its state, session and account |
| `usage [--since 24h\|7d\|ISO \| --since-last] [--by harness\|provider\|model\|account\|project\|branch\|session\|day] [--no-auxiliary] [--json] [--mark]` | Token usage over a time range, broken down. Counts are known sums: requests with a field unknown are counted in UNKNOWN, never as 0. By account, project or branch it shows the attributed, ambiguous and unattributed shares, and every group adds up to the total. `--since-last` starts where you last closed this view, and records this look; piped or `--json` output records nothing unless `--mark` |
| `dashboard [--watch]` | Live terminal view of every pool (bars, reset countdowns, age) and every agent's state. `--watch` also collects, in place of `run` |
| `run [--once] [--interval N]` | Collects into the store until stopped (Ctrl-C), without a screen. One per machine |
| `doctor [--capture PANE]` | Checks the setup, whether a collector is running, and each collector's last run, last new data and last error (field names only). `--capture` prints a pane's screen with personal details removed, for recording an adapter fixture |
| `init --claude-statusline [--yes] [--undo]` | Wraps Claude Code's status line with `usage-watch statusline-tap`, after showing the change; `--undo` restores the backup |

Every command exits 0 on success and 1 on failure. Errors say what failed,
what was expected, and the fix.

## Safety

- It never types into a terminal. It reads screens; it does not send keys.
- It never reads a token, key or credential file, and never stores prompts
  or other content.
- When it cannot tell, it says unknown: a screen it doesn't recognise is
  `unknown`, an account it can't attribute is unattributed, and an unknown
  count is never shown as 0.

`usage-watch primer` lists these guarantees in full.

## Harness support

| Harness | Limit message | Busy, idle and typing | Evidence |
|---|---|---|---|
| omp | verified | verified | live stalled, busy and idle panes |
| Claude Code | verified wording | verified | 2.1.285 binary strings, live busy pane; no live stall recorded yet |
| Codex | verified wording | **unverified** | real session log, 0.154.0 binary; stalled only when the screen is unambiguous |

To add or correct a harness, capture a real screen with
`usage-watch doctor --capture PANE`, save it under `tests/fixtures/`, and
write or adjust the module in `src/usage_watch/adapters/`. Each adapter
answers one question, which state this screen is in, and is tested against
recorded screens.

## Configuration

None is needed. An optional `~/.config/usage-watch/config.toml` (or
`$USAGE_WATCH_CONFIG`) may set:

```toml
[defaults]
interval = 300        # seconds between scans
```

A config file from an earlier version may still hold nudge settings; they
are ignored.

## Develop

```sh
uv sync
uv run pytest
```

## Licence

0BSD. Use it for anything, no attribution required.

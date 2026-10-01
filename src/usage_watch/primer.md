# usage-watch primer

## What it is for

AI coding agents (Claude Code, Codex, omp) run for hours in tmux panes. When a
subscription's usage limit runs out, most of them print an error and stop at
their prompt. They stay stopped after the limit resets, until someone types.
When several agents run overnight or across projects, that wait is often the
biggest delay in the work.

usage-watch is the someone who types. It finds agents that stopped on a usage
limit, waits until the pool they draw on has refilled, and types `continue`
into each one.

## How it works

`usage-watch run` is the collector. It keeps a local SQLite store
(`~/.local/state/usage-watch/usage.db`) and, every few seconds:

1. **Discover.** It reads every pane in every tmux session, walks the process
   tree under each pane to see which harness is running, and asks git and
   workmux which project, worktree and lane each pane belongs to. Nothing is
   configured per project, and new panes, lanes and projects are picked up
   automatically. Each harness's own session files say which pane runs
   which session.
2. **Detect.** A harness adapter reads each agent's screen and names its state.
3. **Collect.** It reads token usage from each harness's session log, and
   pool capacity from sources that need no credential: Claude Code's status
   line (once `usage-watch init --claude-statusline` installs the tap),
   Claude's own cached utilization, Codex's `rate_limits` in its session
   files, and omp's usage cache. Every reading keeps its source and age.
4. **Decide and nudge.** For a stalled pane, the nudge policy finds the
   session's account and checks that a fresh reading, or the limit's own
   reset, shows the blocking window clear, and that no other window is known
   to block. Then it types the nudge into the pane and presses Enter.
   Otherwise the pane waits, with the reason recorded. `run --no-nudge`
   collects only.

Everything else reads the store. `usage-watch status` shows whether a
collector is running and how old its data is, each pool, and each agent.
`usage-watch usage` breaks token usage down by harness, provider, model,
account, project, branch, session or day; `--since-last` shows what happened
since you last looked. `usage-watch dashboard` shows pools and agents,
refreshing live; with `--watch` it hosts the collector and does the nudging
as well, in place of `run`.

## How it sees your setup

Each agent pane gets a role:

- **lane**: running in a git worktree, usually a workmux lane.
- **orchestrator**: running in the main checkout of a project that has lanes.
- **standalone**: anything else.

A lane that has written `.workmux/HANDOFF.md` has finished, and is never
nudged. Roles are used to choose nudge text. Run `usage-watch map` to see the
map it has built.

## Pane states

- **busy**: the agent is working.
- **idle**: waiting for input for some reason other than a limit.
- **stalled**: stopped on a usage limit and waiting for someone to type. Only
  this state is ever nudged.
- **resuming**: stopped on a limit, but the harness will continue by itself
  (Claude Code can do this). Left alone.
- **typing**: there is text in the input box. Never typed over.
- **unknown**: the screen did not match what the adapter knows. Never nudged.

A stalled pane's action is **nudge** (the pool has capacity), **wait** (it
does not yet), or **escalate** (something needs a person; see below).

## Guarantees

- It never types into a pane unless the adapter reads it as `stalled`.
- It never types into an input box that already has text in it.
- It never nudges unless the pane's account is known and a fresh reading
  (or the limit's own reset) shows the blocking window clear, with no other
  window known to block. An estimate never authorises a nudge.
- It nudges each stall once. If the same stall is still there afterwards, or
  a pane keeps stalling while the pool shows capacity, it escalates instead
  of retrying: a log line starting `ESCALATE` and a desktop notification.
- One collector runs per machine (a lock file enforces it), so no pane is
  nudged twice.
- It never reads a token, key or credential file, and never stores prompts.
- An unknown count is never shown as 0: sums say how many requests had a
  field unknown, and shares of usage say how much is unattributed.
- When unsure, it does nothing. A missed nudge costs a wait. A wrong one can
  derail an agent.

## Limits

- It only sees agents running inside tmux. It cannot see or type into plain
  terminal windows or IDE panels.
- It only knows the pools its credential-free sources report, and only as
  fresh as they are. Claude capacity is fresh only with the status line tap
  installed; otherwise it is as old as Claude Code's own cached copy, and
  stale readings make the policy wait. Codex panes aren't joined to their
  session yet, so their account stays unknown and they wait.
- Nothing is collected unless `usage-watch run` (or `dashboard --watch`) is
  running. Claude keeps only about 30 days of transcripts, so history older
  than that is lost unless it was collected.
- The screen shows the model, not the account. With two accounts of the same
  family (say a personal and a team Claude plan), config must say which
  harness uses which. `usage-watch init` sets that up.
- Adapters read screens, and screens change between harness versions. How
  certain each adapter is appears in `usage-watch doctor`. The Codex screen
  layout has not been verified on a live pane yet.

## For agents

- Check before starting expensive work: `usage-watch status --json`. It has
  `liveness` (`state` is `running`, `stalled` or `none`, with a `message`),
  `pools` (each with `account_label`, `window`, `remaining_pct`,
  `resets_at`, `source`, `age_s` and `stale`) and `agents` (each with
  `pane`, `harness`, `state`, `session_key` and `account_label`).
- See where tokens went: `usage-watch usage --json --by project --since 7d`.
  `total` equals the sum of `rows`; `unknown_requests` counts requests with
  a field unknown.
- Wait for capacity instead of polling:
  `usage-watch wait --provider <id> [--timeout SECONDS]`. It exits 0 once
  the pool has capacity, and 1 on timeout or error.
- Every command exits 0 on success and 1 on failure. Errors go to stderr in
  three parts, `what failed`, `expected:` and `fix:`. Do the fix; do not work
  around the check.
- While `usage-watch run` is active, do not nudge panes yourself. If you
  must, `usage-watch nudge <pane>` applies the same safety checks and the
  same nudge policy.
- If you stop on a usage limit yourself, stop at your prompt. usage-watch
  will type `continue` (or the configured nudge) when your pool refills.
  Resume from where you were.

## Where things live

- Config: `~/.config/usage-watch/config.toml` (or `$USAGE_WATCH_CONFIG`).
  Overrides only.
- Store, log and lock: `~/.local/state/usage-watch/` (or
  `$XDG_STATE_HOME/usage-watch/`).
- Adapters: one module per harness in `usage_watch/adapters/`. Each is tested
  against recorded screens. Use `usage-watch doctor --capture <pane>` to
  record a screen when a harness changes.

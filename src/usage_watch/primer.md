# usage-watch primer

## What it is for

AI coding agents (Claude Code, Codex, omp) run for hours in tmux panes,
often several at once across projects, drawing on subscription pools that
run out and reset on their own schedules. usage-watch is a terminal
observability tool for that setup. It answers: where did my tokens go, how
much capacity does each account have left and when does it reset, and what
is each agent doing right now, including which ones are stalled on a usage
limit and when the provider says they can retry.

It only watches. It never types into a terminal. Automatic nudging (typing
`continue` into a stalled agent once its pool refills) was removed for now;
it may return later, under design D8.

## How it works

`usage-watch run` is the collector. It keeps a local SQLite store
(`~/.local/state/usage-watch/usage.db`) and, every few seconds:

1. **Discover.** It reads every pane in every tmux session, walks the process
   tree under each pane to see which harness is running, and asks git and
   workmux which project, worktree and lane each pane belongs to. Nothing is
   configured per project, and new panes, lanes and projects are picked up
   automatically. Each harness's own session files say which pane runs
   which session.
2. **Detect.** A harness adapter reads each agent's screen and names its
   state. For a stalled pane it also records the retry time the provider
   stated, where there is one (omp's `retry-after-ms`).
3. **Collect.** It reads token usage from each harness's session log, and
   pool capacity from sources that need no credential: Claude Code's status
   line (once `usage-watch init --claude-statusline` installs the tap),
   Claude's own cached utilization, Codex's `rate_limits` in its session
   files, and omp's usage cache. Every reading keeps its source and age.
4. **Alert.** When a pool falls below 20% or 5% remaining, it raises a pool
   alert.

Everything else reads the store. `usage-watch status` shows whether a
collector is running and how old its data is, each pool, and each agent.
`usage-watch usage` breaks token usage down by harness, provider, model,
account, project, branch, session or day; `--since-last` shows what happened
since you last looked. `usage-watch dashboard` shows pools and agents,
refreshing live; with `--watch` it hosts the collector as well, in place of
`run`. `usage-watch doctor` checks the setup and each collector's health.

## How it sees your setup

Each agent pane gets a role:

- **lane**: running in a git worktree, usually a workmux lane.
- **orchestrator**: running in the main checkout of a project that has lanes.
- **standalone**: anything else.

## Pane states

- **busy**: the agent is working.
- **idle**: waiting for input for some reason other than a limit.
- **stalled**: stopped on a usage limit and waiting for someone to type.
  Shown with the provider's own retry time when it gave one.
- **resuming**: stopped on a limit, but the harness will continue by itself
  (Claude Code can do this).
- **typing**: there is text in the input box.
- **unknown**: the screen did not match what the adapter knows.

## Guarantees

- It never types into a terminal. It reads screens and files; it never
  sends keys to a pane.
- It never reads a token, key or credential file. Limits come only from
  what the harnesses hand out themselves.
- It never stores prompts or other conversation content: only counts,
  states and the metadata needed to attribute them.
- Unknowns fail safe. A screen it doesn't recognise is `unknown`, not
  guessed. An unknown count is never shown as 0: sums say how many requests
  had a field unknown, and shares of usage say how much is unattributed.
  A stale capacity reading is marked STALE, never passed off as fresh.
- One collector runs per machine (a lock file enforces it).

## Limits

- It only sees agents running inside tmux. It cannot see plain terminal
  windows or IDE panels.
- It only knows the pools its credential-free sources report, and only as
  fresh as they are. Claude capacity is fresh only with the status line tap
  installed; otherwise it is as old as Claude Code's own cached copy. Codex
  panes aren't joined to their session yet, so their account stays unknown.
- Nothing is collected unless `usage-watch run` (or `dashboard --watch`) is
  running. Claude keeps only about 30 days of transcripts, so history older
  than that is lost unless it was collected.
- It does not restart stalled agents. It shows them; resuming one is up to
  you or the harness.
- Adapters read screens, and screens change between harness versions. How
  certain each adapter is appears in `usage-watch doctor`. The Codex screen
  layout has not been verified on a live pane yet.

## For agents

- Check capacity before starting expensive work: `usage-watch status --json`.
  It has `liveness` (`state` is `running`, `stalled` or `none`, with a
  `message`), `pools` (each with `account_label`, `window`,
  `remaining_pct`, `resets_at`, `source`, `age_s` and `stale`) and `agents`
  (each with `pane`, `harness`, `state`, `session_key` and
  `account_label`).
- See where tokens went: `usage-watch usage --json --by project --since 7d`.
  `total` equals the sum of `rows`; `unknown_requests` counts requests with
  a field unknown.
- Every command exits 0 on success and 1 on failure. Errors go to stderr in
  three parts, `what failed`, `expected:` and `fix:`. Do the fix; do not work
  around the check.
- usage-watch will not resume you. If you stop on a usage limit, nothing
  types `continue` for you: a person, or the harness itself, has to.

## Where things live

- Config: `~/.config/usage-watch/config.toml` (or `$USAGE_WATCH_CONFIG`).
  Optional; only `[defaults] interval` is read.
- Store, log and lock: `~/.local/state/usage-watch/` (or
  `$XDG_STATE_HOME/usage-watch/`).
- Adapters: one module per harness in `usage_watch/adapters/`. Each is tested
  against recorded screens. Use `usage-watch doctor --capture <pane>` to
  record a screen when a harness changes.

# usage-watch primer

## What it is for

AI coding agents (Claude Code, Codex, omp) run for hours in tmux panes. When a
subscription's usage limit runs out, most of them print an error and stop at
their prompt. They stay stopped after the limit resets, until someone types.
When several agents run overnight or across projects, that wait is often the
biggest delay in the work.

usage-watch is the someone who types. It finds agents that stopped on a usage
limit, waits until OpenUsage says the pool they draw on has refilled, and
types `continue` into each one.

## How it works

Every scan (five minutes by default) does four things:

1. **Discover.** It reads every pane in every tmux session, walks the process
   tree under each pane to see which harness is running, and asks git and
   workmux which project, worktree and lane each pane belongs to. Nothing is
   configured per project. The map is rebuilt on every scan, so new panes,
   new lanes and new projects are picked up automatically.
2. **Detect.** A harness adapter reads each agent's screen and names its state.
3. **Confirm.** For a stalled pane, it works out which subscription the pane
   draws on (harness plus model family) and asks OpenUsage whether that pool
   has capacity again.
4. **Nudge.** When it does, it types the nudge into the pane, presses Enter,
   and checks the pane started working.

To watch it happen, `usage-watch dashboard` shows every pool's limits and
reset countdowns above every agent's state, refreshing live. With `--watch`,
the dashboard does the nudging as well, in place of `run`.

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
- It never nudges before OpenUsage confirms the pool has capacity.
- It nudges each stall once. If the same stall is still there afterwards, or
  a pane keeps stalling while the pool shows capacity, it escalates instead
  of retrying: a log line starting `ESCALATE` and a desktop notification.
- One watcher runs per machine (a lock file enforces it), so no pane is
  nudged twice.
- When unsure, it does nothing. A missed nudge costs a wait. A wrong one can
  derail an agent.

## Limits

- It only sees agents running inside tmux. It cannot see or type into plain
  terminal windows or IDE panels.
- It only knows the pools OpenUsage reports. A pane whose provider OpenUsage
  does not track escalates instead of being nudged.
- The screen shows the model, not the account. With two accounts of the same
  family (say a personal and a team Claude plan), config must say which
  harness uses which. `usage-watch init` sets that up.
- Adapters read screens, and screens change between harness versions. How
  certain each adapter is appears in `usage-watch doctor`. The Codex screen
  layout has not been verified on a live pane yet.

## For agents

- Check before starting expensive work: `usage-watch status --json`. Each
  entry has `pane`, `harness`, `role`, `project`, `state`, `provider`,
  `capacity.ok`, `capacity.why`, `action` and `reason`.
- Wait for capacity instead of polling:
  `usage-watch wait --provider <id> [--timeout SECONDS]`. It exits 0 once
  the pool has capacity, and 1 on timeout or error.
- Every command exits 0 on success and 1 on failure. Errors go to stderr in
  three parts, `what failed`, `expected:` and `fix:`. Do the fix; do not work
  around the check.
- While `usage-watch run` is active, do not nudge panes yourself. If you
  must, `usage-watch nudge <pane>` applies the same safety checks.
- If you stop on a usage limit yourself, stop at your prompt. usage-watch
  will type `continue` (or the configured nudge) when your pool refills.
  Resume from where you were.

## Where things live

- Config: `~/.config/usage-watch/config.toml` (or `$USAGE_WATCH_CONFIG`).
  Overrides only.
- Log and lock: `~/.local/state/usage-watch/`.
- Adapters: one module per harness in `usage_watch/adapters/`. Each is tested
  against recorded screens. Use `usage-watch doctor --capture <pane>` to
  record a screen when a harness changes.

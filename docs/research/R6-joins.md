# R6: How can a usage record be joined to a project and a pane?

Date: 2026-09-30. Evidence: read-only inspection of live processes (`lsof`,
tmux) and of the record key names from [R2](R2-local-token-records.md).

## Joining to a project and branch

| Harness | Fields on the record | Branch |
|---|---|---|
| Claude Code | `cwd`, `gitBranch`, `sessionId`, `version` on every assistant line | yes, recorded at the time |
| Codex CLI | `session_meta.payload`: `id`, `cwd`, `cli_version`, `originator`, `source` (marks subagents), `git{branch, commit_hash, repository_url}`; `turn_context` has a per-turn `cwd` | yes, recorded at the time |
| omp | `session` header: `id`, `cwd`, `version` | **no**: derive it from `cwd` with git when ingesting |

## Joining to a pane

The chain is pane, then process, then session file.

- **Pane to process** is already done by usage-watch's topology scan
  (`pane_pid` and its children). Verified again: a pane's shell PID led to
  its omp child.
- **Claude Code: `~/.claude/sessions/<pid>.json`** (verified). For a live
  Claude process, this file holds `sessionId`, `cwd`, `version`, `status`,
  `updatedAt`, and a `tmux` field in the form `session:@window.%pane`. Its
  pane ID matched the pane's real ID, and its `sessionId` matched a
  transcript file. That's a complete pane, process and session join, and
  Claude records it itself. `lsof` does **not** work for Claude: it opens,
  appends and closes the transcript.
- **omp: `lsof -p <pid>`** (verified on four live processes). Each held
  exactly one session `.jsonl` open for writing, matching its cwd.
- **Codex: `lsof -p <pid>`** (verified on a desktop app-server process,
  which held its rollout file open; **inferred** for the terminal UI in
  tmux, since none was running).

## Consequences for the plan

- **C4 enrichment is viable**:
  - Claude joins through its session file.
  - omp and Codex join through `lsof`.
  - Project and branch come from the records themselves (Claude, Codex),
    or from git at ingest (omp).
- **Historical records**, from panes that have since closed, join to a
  project by `cwd` but not to a pane. That's fine: pane is an event-level
  detail, not a key for grouping history (plan principles on
  cardinality).
- **`lsof` is macOS and Linux only.** On Linux, `/proc/<pid>/fd` gives the
  same answer without `lsof` installed.
- The Claude session file's `tmux` field is a second, independent check on
  the topology scan's pane attribution.

## Open

1. Does the Codex terminal UI in tmux hold its rollout open, and does it
   write a pid-to-session file the way Claude does?
2. omp has no branch field, so deriving it from `cwd` at ingest can be
   wrong if a worktree changed branch since. How much does that matter?

# D2: Joins

Status: draft. Rests on [R6](../research/R6-joins.md),
[R7](../research/R7-logins-and-accounts.md) and
[R1's live capture](../research/R1-native-otel.md#live-capture-2026-10-01).

The value of usage-watch is joining facts that no single source holds.
Each join below states its method, the confidence it yields, and what
happens when it fails.

## Usage to session

Every usage source names its session: Claude's `sessionId`, Codex's
`session_meta.payload.id` / OTel `conversation.id`, and omp's session header
`id` / `gen_ai.conversation.id`. **Authoritative.** A usage record with no
session ID is stored with `session_id = null` and counted, not dropped.

## Session to project and branch

- **Project:** from `cwd`, through the same git logic as today's topology
  scan (main checkout, or worktree). **Authoritative** for the cwd, and
  **inferred** for the project name.
- **Branch:** from the record where it's there (Claude `gitBranch`, Codex
  `git.branch`), **authoritative**. For omp, from git at ingest time,
  **inferred**: a worktree may have switched branch since.

## Session to pane (live only)

Tried in this order, stopping at the first that works:

| Method | Harness | Confidence |
|---|---|---|
| OTel resource attribute `usage_watch.pane`, set at launch by `usage-watch exec` | all | authoritative |
| Hook or tap run inside the harness, reading `TMUX_PANE` | all with hooks or taps | authoritative |
| `~/.claude/sessions/<pid>.json`, whose `tmux` field names the pane | Claude | authoritative |
| `~/.omp/agent/terminal-sessions/tmux-%N` → session file | omp | authoritative |
| The pane's process, then its open session file (`lsof`, or `/proc/<pid>/fd` on Linux) | omp, Codex | observed |

The pane is stored on the session and on events while live. History is
grouped by project, account and session, never by pane, since pane IDs are
reused.

## Session to account

Tried in this order:

| Method | Harness | Confidence |
|---|---|---|
| OTel identity attributes (`user.account_uuid` + `organization.id`; Codex `user.account_id`), hashed on receipt | Claude, Codex | authoritative |
| Per-request `credentialId`, mapped through omp's `auth_credentials` table to its `identity_key` column (only that column is read) | omp | authoritative |
| Transcript owner fields (`ownerAccountUuid`, `ownerOrganizationUuid`) | Claude, when present | authoritative |
| The account signed in at the harness's home now: `~/.claude.json` `oauthAccount` for the home named by `CLAUDE_CONFIG_DIR` or the default | Claude | inferred: a re-login since the session started would mislead |

**Decision:** Codex sessions without telemetry get `account = null`. The
only other place Codex records its account is its credential file, which
usage-watch doesn't open (plan principle).

## A failed join

A join that fails leaves the field null and records why, in `join_note`:
e.g. `no-session-id`, `pane-closed`, `account-unknown:codex-no-otel`. It is
never guessed silently. Views show the unattributed share as its own line
("unattributed: 12%") rather than hiding it.

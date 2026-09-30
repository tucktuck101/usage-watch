# D2: Joins and attribution

Status: draft, revised after review (2026-10-01). Rests on
[R6](../research/R6-joins.md), [R7](../research/R7-logins-and-accounts.md),
[R1's live capture](../research/R1-native-otel.md#live-capture-2026-10-01).
Scope markers as in [D1](D1-model.md).

Every join produces an **attribution** (D1): a value, the method, a
confidence, and a validity class.

## Validity classes [F]

| Class | Meaning | May be applied to |
|---|---|---|
| `historical` | From the record itself, true whenever it's read | any record, including backfill |
| `live` | From the machine's current state | only sessions that are **live**: seen in a running harness process during this collection pass |
| `time_bounded` | From current state, but valid only within a known window, e.g. "the login at this home now", valid only if the session started after the login last changed | live sessions, and only while the bound can be checked |

**A backfilled record never inherits present-day context.** Backfill gets
`historical` attributions only. Everything else is left null, with the
reason noted.

## Project identity [F]

Three separate things:

| | Derived from | Stable across | Notes |
|---|---|---|---|
| `checkout_id` | a keyed hash (D5) of the real path of the git **common directory** | worktrees of one clone | **Changes if the repository is moved or cloned again**, which starts a new checkout. Non-git directories get a hash of their real path, marked `non_repo` |
| `repository_id` | a keyed hash of the normalised `origin` remote (host and path, lowercased, without scheme, credentials or `.git`) | moves, re-clones, other machines' checkouts of the same remote | null when there's no remote. Two checkouts with the same `repository_id` are linked |
| `project` (display) | the main checkout's directory name, with its parent added when two checkouts would otherwise show the same name | nothing; a label | never used as a key |

Views group by `repository_id` where one exists, else by `checkout_id`.
Remote URLs and paths stay local (D5).

## Joins, in order of preference

### Usage to session [F]

From the record: Claude `sessionId`, Codex `session_meta.payload.id` /
OTel `conversation.id`, omp session `id` / `gen_ai.conversation.id`.
`historical`, `authoritative`. A record without one keeps
`session_id = null`, and is counted.

### Session to checkout and repository [F]

From the session's `cwd` (on the record: `historical`), through git.
- The git lookup is `live` if the directory no longer exists or has
  changed repository. In that case, backfill gets `checkout_id` from the
  path hash only, marked `inferred`.

### Branch [F]

- **Claude and Codex:** from the record (`gitBranch`, `git.branch`).
  `historical`, `authoritative`.
- **omp:** its records have no branch. Current git state is used **only for
  live sessions**, marked `live`, `inferred`. **Backfilled omp records get
  `branch = null`.** A confidently wrong branch is worse than an unknown
  one.

### Session to pane [F]

Pane is always `live`.

| Method | Harness | Confidence |
|---|---|---|
| OTel resource `usage_watch.pane`, set at launch (H3) | all | authoritative |
| A hook or tap inside the harness, reading `TMUX_PANE` | all with hooks or taps | authoritative |
| `~/.claude/sessions/<pid>.json` `tmux` field | Claude | authoritative |
| `~/.omp/agent/terminal-sessions/tmux-%N` → session file | omp | authoritative |
| The pane's process, then its open session file (`lsof`, or `/proc/<pid>/fd`) | omp, Codex | observed |

History is never grouped by pane: pane IDs are reused.

### Session to account [F]

The value is a canonical account via D7's aliases.

| Method | Harness | Validity | Confidence |
|---|---|---|---|
| OTel identity (`user.account_uuid` + `organization.id`; Codex `user.account_id`) | Claude, Codex | historical | authoritative |
| Per-request `credentialId`, mapped through omp's `identity_key` column | omp | historical | authoritative |
| Transcript owner fields | Claude, when present | historical | authoritative |
| The login at the harness's home now (`~/.claude.json` `oauthAccount`) | Claude | time_bounded (valid only if the file hasn't changed since the session started) | inferred |

- **Codex without telemetry** gets `account = null`. The only other record
  of its account is its credential file, which usage-watch doesn't open.
- **Precedence for a session's account:** the first authoritative method
  that yields one. Conflicting authoritative values are both kept, and
  `doctor` reports them.

### Role and task [X]

- **Role:** from workmux or topology (`lane`, `orchestrator`,
  `standalone`), `live`.
- **Task:** from context events (phase 8), `historical` once recorded.

## A failed join [F]

The attribution is absent, and a `join_note` records why, e.g.
`backfill-live-only`, `no-session-id`, `pane-closed`,
`account-unknown:codex-no-otel`. Views show the unattributed share as its
own line. Invariant 1 in D1 makes that share add up.

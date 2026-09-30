# D2: Joins and attribution

Status: draft, revised after third review (2026-10-01). Rests on
[R6](../research/R6-joins.md), [R7](../research/R7-logins-and-accounts.md),
[R1's live capture](../research/R1-native-otel.md#live-capture-2026-10-01).
Scope markers as in [D1](D1-model.md).

Every join produces **attribution evidence** (table
`attribution_evidence`): a value for one dimension of one subject, with the
method, source, confidence, validity class, `valid_from`, `valid_to`,
`first_observed_at` and `last_confirmed_at`. Every piece of evidence is
kept. Seeing the same evidence again updates `last_confirmed_at` and adds
no row. A new value from the same method is a new row, and the previous
row's `valid_to` is closed.

What views, totals and the nudge policy use is the **effective
attribution** (below), resolved from the evidence.

Subjects are `session`, `usage_event`, `capacity_sample` and `limit_event`.
Sessions are referred to by `session_key` (`"<harness>:<session_id>"`)
everywhere.

## Validity classes [F]

| Class | Meaning | `valid_from`, `valid_to` | May be recorded for |
|---|---|---|---|
| `historical` | From the record itself, true whenever it's read | both null: holds for the subject at any time | any subject, including backfill |
| `live` | From the machine's current state | `valid_from = first_observed_at`; `valid_to = last_confirmed_at`, closed when no longer confirmed | only subjects that are **live**: seen in a running harness process during this collection pass |
| `time_bounded` | From current state, but holding only within an explicit span | `valid_from` is when that state was last seen to change; `valid_to` is null while it's current, and closed when the same method sees a new value | only while `valid_from` is knowable. Otherwise the method records `live` evidence instead |

**How each time-bounded method sets its bounds:**

| Method | `valid_from` | `valid_to` | If `valid_from` isn't knowable |
|---|---|---|---|
| Claude current login (`<home>/.claude.json` `oauthAccount`) | the `first_observed_at` of this login value, when usage-watch had already seen that home with a different login (so the change is known to fall before it) | closed when a different login is first seen at that home | the evidence is `live` only: recorded for live sessions of that home, never for backfill |

By resolution rule 1 below, time-bounded and live evidence counts for a
session only if the session's span lies inside `[valid_from, valid_to]`.

**A backfilled record never inherits present-day context.** Backfill gets
`historical` evidence only. Every other dimension stays `unattributed`,
with the reason noted.

## Effective attribution [F]

Table `effective_attributions`: at most one row per
`(subject_kind, subject_id, dimension)`. A row exists for a subject with
its own evidence for that dimension, or after an attribution attempt for
that subject and dimension that found no evidence (see
[A failed join](#a-failed-join-f)). It is recomputed whenever that
subject's evidence changes. Inherited values are computed at query time.
It holds a `state` (`attributed`, `ambiguous` or `unattributed`), a
`value` (set only when `attributed`), the `confidence` and `evidence_id`
it rests on, and a `note`.

**A usage event's time and session** are `usage_events.observed_at` and
`usage_events.session_key`. Both are copied from the event's accounting
observation when the event is created, and never change.

**Resolution rule:**
1. Only evidence valid at the subject's time counts: for a usage event,
   `usage_events.observed_at`; for a session, its span; for a capacity
   sample or limit event, its `observed_at`.
2. Take the highest confidence present (`authoritative` > `observed` >
   `inferred`).
3. If every value at that confidence agrees, the state is `attributed`.
4. If they disagree, the state is `ambiguous`. Never a guess, and never a
   fall back to a lower confidence. `doctor` reports the disagreement.
5. No evidence means `unattributed`.

**Inheritance:** a usage event's effective attribution for a dimension is
its own, if it has any evidence for that dimension, else that of the
session named by `usage_events.session_key`.

**Totals:** each usage event counts **once**, under its single effective
value, or under `ambiguous` or `unattributed`. Conflicting authoritative
evidence never makes an event appear twice.

## Project identity [F]

Three separate things:

| | Derived from | Stable across | Notes |
|---|---|---|---|
| `checkout_id` | a hash, keyed with the per-install secret (D5), of the real path of the git **common directory** | worktrees of one clone | **Changes if the repository is moved or cloned again**, which starts a new checkout. Non-git directories get a keyed hash of their real path, marked `non_repo` |
| `repository_id` | a hash, keyed with the per-install secret (D5), of the normalised `origin` remote (host and path, lowercased, without scheme, credentials or `.git`) | moves and re-clones **on this installation** | **Not comparable across machines or installations.** Null when there's no remote. Two checkouts with the same `repository_id` are linked |
| `project` (display) | the main checkout's directory name, with its parent added when two checkouts would otherwise show the same name | nothing; a label | never used as a key |

A lost per-install secret changes both `checkout_id` and `repository_id`.

Views group by `repository_id` where one exists, else by `checkout_id`.
Remote URLs and paths stay local (D5).

## Joins, in order of preference

### Usage to session [F]

From the record: Claude `sessionId`, Codex `session_meta.payload.id` /
OTel `conversation.id`, omp session `id` / `gen_ai.conversation.id`,
formed into a `session_key`. `historical`, `authoritative`. A record
without one keeps `session_key = null`, and is counted.

### Session to checkout and repository [F]

From the session's `cwd` (on the record: `historical`), through git.
- **Backfill [needed before C4, not F1]:** a historical checkout identity
  is never manufactured from current state. If it can't be established
  for the session's time (the directory has moved, no longer exists, or
  now belongs to a different repository), `checkout` is `unattributed`,
  with a `note` giving the reason. There is no path-hash fallback.
- The historical `cwd` and the display label are kept separately, as the
  session's `cwd` field and its `project` display label. Neither is a
  checkout identity.

### Branch [F]

- **Claude and Codex:** from the record (`gitBranch`, `git.branch`).
  `historical`, `authoritative`.
- **omp:** its records have no branch. Current git state is used **only for
  live sessions**, marked `live`, `inferred`. **Backfilled omp records stay
  `unattributed` for branch.** A confidently wrong branch is worse than an
  unknown one.

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
| The login at the harness's home now (`<home>/.claude.json` `oauthAccount`) | Claude | time_bounded, bounds as above; else live | inferred |
| Config fallback (`[accounts.<harness>]`, D7), method `config` | any | live | inferred |

- **Codex without telemetry** has no account evidence and stays
  `unattributed`. The only other record of its account is its credential
  file, which usage-watch doesn't open.
- **A session's account** is its effective attribution, by the resolution
  rule. Disagreeing authoritative values make it `ambiguous`, reported by
  `doctor`. The session's usage is then counted once, under `ambiguous`,
  never under both accounts.

### Capacity samples and limit events to account [F]

Capacity samples and limit events are attribution subjects for `account`
(`subject_kind` `capacity_sample`, `limit_event`). The nudge policy (D8)
and views read their effective account.

| Record | Method | Validity | Confidence |
|---|---|---|---|
| omp usage-cache anchor | the report's account identifiers, resolved to a canonical account through its alias (D7) | historical | observed |
| Claude status-line anchor | its session's **effective** account (the status-line input names the session) | as the session evidence it rests on | the session's effective confidence |
| Codex `rate_limits` anchor | its rollout's session's **effective** account | as the session evidence it rests on | the session's effective confidence |
| Transcript limit event (Claude) | its session's **effective** account | as the session evidence it rests on | the session's effective confidence |

- Evidence taken from a session is produced only when the session's
  effective account is `attributed`. A session that is `ambiguous` or
  `unattributed` gives its samples and events no account evidence, so they
  are `unattributed`.
- **Coherence:** evidence derived from a session is re-derived whenever the
  session's effective account changes, and the sample's or event's
  effective attribution recomputed.
- **Coherence:** a harness whose identity source failed on its last pass
  counts as `partial` for account discovery (D7) until it succeeds again.
- A capacity or limit source not listed here produces no account evidence
  until a method is added for it.

### Session to billing route [F]

`billing_route` (`api_key`, `subscription`) is session-level evidence.
Codex OTel can't be linked by request to the rollout, which has no request
ID, so its `auth_mode` becomes evidence on the **session**, linked by
`conversation.id` = `session_id`. `historical`, `authoritative`. Usage
events inherit it. Claude and omp have no source yet, so theirs is
`unattributed`, shown as `unknown` (D1).

### Role and task [X]

- **Role:** from workmux or topology (`lane`, `orchestrator`,
  `standalone`), `live`.
- **Task:** from context events (phase 8), `historical` once recorded.

## A failed join [F]

No evidence is recorded. An attribution attempt that finds no evidence
writes an `effective_attributions` row with `state = unattributed`,
`value = null`, `evidence_id = null`, and `note` = the reason, e.g.
`backfill-live-only`, `no-session-id`, `pane-closed`,
`account-unknown:codex-no-otel`, `checkout-not-established`. An
`ambiguous` row also carries a `note`. Views show the
`unattributed` and `ambiguous` shares as their own lines. Invariant 1 in
D1 makes the shares add up.

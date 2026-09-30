# D3: Storage and runtime

Status: draft, revised after third review (2026-10-01). Scope markers as
in [D1](D1-model.md).

## Engine and place [F]

- SQLite from the standard library, at
  `$XDG_STATE_HOME/usage-watch/usage.db` (default
  `~/.local/state/usage-watch/usage.db`).
- **WAL mode**, so readers never block the writers and the writers never
  block readers.
- Local only. Nothing leaves the machine unless export (E1) is switched on.

## Four parts, kept separate [F]

| Part | Does | Owns (writes) |
|---|---|---|
| **Collector runtime** | runs collectors (D6), reconciles observations, resolves effective attributions, runs migrations | **every data table**, plus `runtime`, `watermarks`, `collector_status` and `schema_version` |
| **Nudge policy** | reads the store and screens, decides and types nudges (D8) | `nudges` only |
| **Dashboard** | reads the store, draws | its own `looks` row only |
| **Queries** (`status`, `usage`, …) | read the store | their own `looks` row only |

- **One owner per table.** No table, and no `looks` row, has two writers,
  so there are no conflicting writes.
- **Every write is a short transaction**, with a busy timeout of 5 s.
- **Schema migrations run only in the collector runtime**, while it holds
  the lock (below).
- History collection doesn't depend on nudging or on the dashboard.
- For the proof of concept the parts share one process: `usage-watch run`
  hosts the collector runtime, with the nudge policy on by default and
  `--no-nudge` to collect only. Ownership stays per part, not per process.
- `dashboard --watch` hosts the same pair.
- The one-per-machine lock belongs to the **collector runtime**, not to
  nudging.
- A separate `usage-watch collect` daemon can come later [L] without
  changing any of this.

## Is a collector running? [F]

**The lock is authoritative.** The collector runtime holds an exclusive
lock on `run.lock` for as long as it runs. A reader checks it with a
non-blocking test and never waits on it.

**The heartbeat adds a second signal.** The runtime keeps a row in
`runtime`:

| Field | Meaning |
|---|---|
| `runtime_id` | this run of the runtime |
| `pid` | its process ID |
| `started_at` | when it started |
| `heartbeat_at` | updated every 10 s |
| `version` | the usage-watch version running |

A reader combines the two:

| Lock | Heartbeat | Reader says |
|---|---|---|
| held | under 30 s old | **collector running** |
| held | 30 s or older, or missing | **collector stalled** |
| not held | any | **no collector running** |

**`collector_status` is about data age, not liveness.** Per collector it
holds:
- when it last ran and when it last succeeded;
- its last error, as field names only (D5);
- the number of unlinked secondary observations (orphans) per source,
  counted by query: link state is derived, not stored (D1).

Every read-only command shows the liveness state and the data's age:

```text
collector running, data as of 14:02:31
collector stalled (pid 4121, last heartbeat 14:03:10); data as of 14:02:31
no collector running; data as of yesterday 18:40; start one with: usage-watch run --no-nudge
```

Stale data is always shown with its age, never passed off as current.

## Tables [F]

The owner of every table below is the collector runtime, except where the
table says otherwise.

| Table | Holds | Key |
|---|---|---|
| `sessions` | sessions (D1): `harness` and `session_id` as fields | `session_key` |
| `usage_observations` | what each source said about each request (D1, D6). No stored link state: `primary`, `linked` and `orphan` are derived (D1). `stream_key` is fixed when first stored. A repeat of an existing identity updates the row under the source's counting rule (D1, D6). `provider_request_key` is nullable, with a **non-unique** index | `observation_id`; unique `(source, stream_key, source_request_key)` |
| `usage_events` | canonical counting events (D1). `accounting_observation_id` references `usage_observations`, NOT NULL. `observed_at` and `session_key` are copied from it and immutable. `reconciled_version` is an integer. Updated in place, never rebuilt | `usage_id`; unique `(accounting_observation_id)` |
| `event_observations` | the other observations behind each event: `role` is `supporting` or `metadata` only (the accounting link is on `usage_events`). `field` is NOT NULL: the field taken, for `metadata`; `''` for `supporting` | `(usage_id, observation_id, role, field)`; partial unique `(observation_id) WHERE role = 'supporting'`; partial unique `(usage_id, field) WHERE role = 'metadata'` |
| `attribution_evidence` | every piece of attribution evidence (D1, D2). `valid_from` NOT NULL, `0` for an unbounded start; `valid_to` nullable, not in the identity | `evidence_id`; unique `(subject_kind, subject_id, dimension, method, value, valid_from)` |
| `effective_attributions` | the resolved attribution: `state`, `value` when `attributed`, `confidence`, `evidence_id`, and `note` (why, when `unattributed` or `ambiguous`, D2). Rows exist for subjects with their own evidence, and after an attempt that found zero evidence (`unattributed`, `value` and `evidence_id` null, `note` the reason). Inherited values are computed at query time | `(subject_kind, subject_id, dimension)` |
| `capacity_samples` | anchors. Account through attribution. `window` NOT NULL, `other:unstated` where the source names none | `capacity_sample_id`; unique `(source, stream_key, window, observed_at)` |
| `limit_events` | hit and reset notices. Account through attribution. `window` nullable, not in the identity | `limit_event_id`; unique `(source, stream_key, source_key)` |
| `cost_events` | D1 cost events. `scope_id` is a `usage_id` or a `session_key`. `price_version` NOT NULL, `''` for bases other than `list_price` | `(scope_kind, scope_id, source, basis, price_version)` |
| `state_samples` | agent states | `(pane, observed_at)` |
| `context_events` | context [X] | id |
| `checkouts` | `checkout_id`, `repository_id`, display name, local path | `checkout_id` |
| `accounts` | D7. `account_key` is a random opaque local ID (128-bit hex), generated when the account is created, never derived from an alias | `account_key` |
| `account_aliases` | D7. Also `evidence`, `confidence`, `verified_by`, `first_seen`, `last_confirmed`, `revoked_at`. Several sources asserting one alias are separate rows; one alias asserted against different accounts is separate rows too (a conflict, D7) | unique `(alias_kind, alias_hash, account_key, asserted_by)` |
| `account_merges` | `merge_id`, `from_key`, `into_key`, `evidence`, `verified_by`, `created_at`, `revoked_at`. The canonical account is the result of following unrevoked merges. Undoing a merge sets `revoked_at`; aliases never move | `merge_id` |
| `watermarks` | how far each collector has read: `(path, inode, byte offset)` per file (D6) | `(collector, file)` |
| `collector_status` | data age and errors per collector (above) | `collector` |
| `runtime` | the heartbeat (above): the current run plus the last 20 | `runtime_id` |
| `looks` | "last looked" (below). **Owner: each view, its own row** | `(who, view)` |
| `nudges` | the nudge policy's decisions and their evidence (D8). **Owner: the nudge policy** | id |
| `schema_version` | one row | |

- Every reference to a session, in any table, is a `session_key`, or null
  when the session is unknown.
- `attribution_evidence` and `effective_attributions` refer to their
  subject by `subject_kind` (`session`, `usage_event`, `capacity_sample`,
  `limit_event`) and `subject_id`.
- `effective_attributions` is recomputed by the collector runtime whenever
  the evidence for that subject and dimension changes.
- **`stream_key`** is fixed when a row is first stored, and never changes.
  For capacity samples and limit events it is the source's own identifier
  for what it reports on, keyed-hashed (D5): the session for the status
  line, Codex `rate_limits` and transcript notices; one omp usage-cache
  entry per login; the Claude home for `cachedUsageUtilization`.
- **No table key includes `account`.** Account comes through attribution.
- **Every declared identity is enforceable under SQLite UNIQUE semantics;
  no identity column is nullable.** SQLite treats NULLs as distinct in a
  UNIQUE index, so every column of every key and unique constraint above
  is NOT NULL, with a sentinel where a value can be absent: `valid_from`
  `0`, `price_version` `''`, `field` `''`, `window` `other:unstated`.
  `stream_key`, `source_request_key`, `source_key`, `value`, `subject_id`,
  `scope_id`, `pane`, `who`, `view`, `collector`, `file` and every
  `account_aliases` identity column are always present, so NOT NULL.
- **Canonical events are stable.** One primary observation permanently
  corresponds to one usage event. Reconciliation updates an event's token
  fields, links and `disagreement` in place, and reprocesses in place any
  event whose `reconciled_version` is below the current one. It never
  deletes and re-creates an event (D1).

### Nudge decisions [F]

- Each decision is a row in `nudges` (D8).
- **Repeated identical `wait` decisions are recorded once, then updated.**
  Identical means the same `(stall_id, reason code)` (D8).
  The row carries `last_seen_at` and a `count`. The policy doesn't write a
  new row each polling interval.
- A `nudge` or `escalate` decision is always a new row.

## Schema changes [F]

- Forward-only migrations numbered in code.
- Run **only by the collector runtime**, at start-up, while holding the
  lock, inside a transaction, with a backup copy taken first.
- A read-only command that finds a newer schema than it knows exits with a
  prompt to upgrade.
- A reader, or the nudge policy, that finds an **older** schema than it
  expects says "the collector hasn't migrated yet" and waits, rather than
  reading.

## Retention [F: mechanism; defaults configurable]

| Records | Kept | Then |
|---|---|---|
| usage observations and usage events, with their `event_observations` links | 400 days | deleted together |
| orphan observations | 400 days | deleted |
| attribution evidence, effective attributions, cost events | with their subject | deleted with it |
| capacity samples | 30 days in full | one per source, stream and window per hour, 400 days |
| limit events, nudges | 400 days | deleted |
| state samples | 7 days | deleted |
| sessions, checkouts, accounts | while referenced | deleted with their last reference |

- Observations are kept as long as events, so reconciliation can be re-run
  over them. An event and its accounting observation are deleted together.
- When a capacity sample is thinned out, its evidence and effective rows go
  with it.
- Continuous collection is what preserves history, because Claude keeps
  only about 30 days of transcripts (R2).
- `runtime` keeps the current run plus the last 20.

## "Last looked" [F]

`looks` has one row per `(who, view)`, written only by that view. `who` is
`cli`, `dashboard` or a name the caller passes. Each row holds three
timestamps:

| Timestamp | Set when |
|---|---|
| `opened_at` | the view is opened |
| `last_seen_at` | each successful full render. For the dashboard, every refresh |
| `closed_at` | a normal close. For one-shot commands, the same moment as `opened_at` |

**V2 "since I last looked" means since the previous look's `closed_at`.**
If the previous look has no `closed_at` because it exited abnormally, its
`last_seen_at` is used instead, and the view says so. A refresh never moves
the marker V2 uses. Views are independent: opening one never moves
another's row. Piped or `--json` output records nothing unless asked with
`--mark`.

## Size

A primary observation, its event and its own attribution evidence come
to about 800 bytes. At 5,000 requests a day that's
about 4 MB a day, or about 1.6 GB over 400 days.

- Effective attribution rows are stored only where an event has its own
  evidence, or where an attempt found none (D1); values inherited from the session are computed at query time,
  so they add nothing per request.
- A secondary source (e.g. OTel) adds about 300 bytes per observation it
  sends.

Retention is configurable for smaller disks.

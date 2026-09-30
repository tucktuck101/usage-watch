# D3: Storage and runtime

Status: draft, revised after review (2026-10-01). Scope markers as in
[D1](D1-model.md).

## Engine and place [F]

- SQLite from the standard library, at
  `$XDG_STATE_HOME/usage-watch/usage.db` (default
  `~/.local/state/usage-watch/usage.db`), in WAL mode.
- Local only. Nothing leaves the machine unless export (E1) is switched on.

## Four parts, kept separate [F]

| Part | Does | Writes the store? |
|---|---|---|
| **Collector runtime** | runs collectors (D6), reconciles observations, writes records | yes, **the only writer** |
| **Nudge policy** | reads the store and screens, decides and types nudges (D8) | only its own decisions |
| **Dashboard** | reads the store, draws | only its `looks` row |
| **Queries** (`status`, `usage`, …) | read the store | only their `looks` row |

- History collection doesn't depend on nudging or on the dashboard.
- For the proof of concept they share one process: `usage-watch run` hosts
  the collector runtime, with the nudge policy on by default and
  `--no-nudge` to collect only.
- `dashboard --watch` hosts the same pair.
- The one-per-machine lock belongs to the **collector runtime**, not to
  nudging.
- A separate `usage-watch collect` daemon can come later [L] without
  changing any of this.

**When no collector is running**, the store's `collector_status` table
tells readers:
- when each collector last ran and succeeded;
- its last error, as field names only (D5).

Every read-only command then shows either
`collector running, data as of 14:02:31`, or
`no collector running; data as of yesterday 18:40; start one with: usage-watch run --no-nudge`.
Stale data is always shown with its age, never passed off as current.

## Tables [F]

| Table | Holds | Key |
|---|---|---|
| `usage_observations` | what each source said about each request | `(source, source_request_key)` |
| `usage_events` | canonical requests (D1) | `usage_id` |
| `attributions` | enrichment per subject and dimension (D1, D2) | `(subject_kind, subject_id, dimension, method)` |
| `capacity_samples` | anchors | `(account, window, source, observed_at)` |
| `limit_events` | hit and reset notices | `(source, source_key)` |
| `cost_events` | D1 cost events | `(scope_kind, scope_id, source, basis, price_version)` |
| `state_samples` | agent states | `(pane, observed_at)` |
| `context_events` | context [X] | id |
| `sessions` | sessions | `(harness, session_id)` |
| `checkouts` | `checkout_id`, `repository_id`, display name, local path | `checkout_id` |
| `accounts`, `account_aliases` | D7 | D7 |
| `watermarks` | how far each collector has read | `(collector, stream)` |
| `collector_status` | liveness for readers | `collector` |
| `looks` | "last looked" (below) | `(who, view)` |
| `nudges` | the nudge policy's decisions and their evidence (D8) | id |
| `schema_version` | one row | |

## Schema changes [F]

- Forward-only migrations numbered in code, run by the writer at start-up
  inside a transaction, with a backup copy taken first.
- A read-only command that finds a newer schema than it knows exits with a
  prompt to upgrade.

## Retention [F: mechanism; defaults configurable]

| Records | Kept | Then |
|---|---|---|
| usage observations and usage events | 400 days | deleted together |
| attributions, cost events | with their subject | deleted with it |
| capacity samples | 30 days in full | one per window per hour, 400 days |
| limit events, nudges | 400 days | deleted |
| state samples | 7 days | deleted |
| sessions, checkouts, accounts | while referenced | deleted with their last reference |

- Observations are kept as long as events, so reconciliation can be re-run
  over them.
- Continuous collection is what preserves history, because Claude keeps
  only about 30 days of transcripts (R2).

## "Last looked" [F]

`looks` has one row per `(who, view)`. `who` is `cli`, `dashboard` or a name
the caller passes. Each row holds three timestamps:

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

A usage observation plus its event and attributions come to about 700
bytes. At 5,000 requests a day that's about 3.5 MB a day, or about 1.4 GB
over 400 days. Retention is configurable for smaller disks.

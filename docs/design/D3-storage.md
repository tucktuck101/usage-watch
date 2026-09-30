# D3: Storage

Status: draft.

## Engine and place

- **SQLite, from the standard library**, in `$XDG_STATE_HOME/usage-watch/usage.db`
  (default `~/.local/state/usage-watch/usage.db`), in WAL mode.
- **One writer:** the collector runtime (`usage-watch run`, or
  `dashboard --watch`), under the existing one-per-machine lock. Every
  command that only reads opens the database read-only. So `status`,
  `usage` and the dashboard never block collection, and a crash can't
  leave two writers.
- Local only. Nothing here leaves the machine unless export (E1) is
  switched on.

## Tables

| Table | Holds | Key |
|---|---|---|
| `capacity_samples` | D1 capacity samples | `(account, window, source, observed_at)` |
| `usage_events` | D1 usage events | `(source, request_key)`, unique, which is what makes re-reading a file harmless |
| `cost_events` | D1 cost events | `(usage_ref, basis)` |
| `state_samples` | agent-state samples | `(pane, observed_at)` |
| `context_events` | context events | id |
| `sessions` | the session entity | `(harness, session_id)` |
| `accounts` | the account registry (D7) | `key` (hashed) |
| `watermarks` | per collector: how far it has read | `(collector, stream)` |
| `looks` | "last looked" markers | `(who, view)` |
| `schema_version` | one row | |

## Schema changes

A `schema_version` row, with forward-only migrations numbered in code and
run by the writer at start-up, inside a transaction. A read-only command
that finds a newer schema than it knows exits with a prompt to upgrade.
There are no down-migrations. A backup copy is taken before each migration.

## Retention

Configurable. The defaults below keep a year of what views need, and a week
of what's only live detail:

| Records | Kept | Then |
|---|---|---|
| usage and cost events | 400 days | deleted |
| capacity samples | 30 days at full detail | one per window per hour, kept 400 days |
| state samples | 7 days | deleted |
| context events, sessions, accounts | while referenced | deleted with their last reference |

Retention runs daily in the writer. The Claude transcript's own retention is
about 30 days (R2), so continuous collection, not occasional backfill, is
what preserves history.

## "Last looked"

`looks` records when a person or an agent last opened a view (`who` is
`cli`, `dashboard` or a name the caller passes). "Since I last looked"
(V2) reads from that marker, and advances it only when the view is shown in
full, not when it's piped or asked for as `--json`.

## Size

Rough guide: one usage event is about 300 bytes including indexes. At 5,000
requests a day, that's about 1.5 MB a day, or about 0.6 GB for 400 days.
Capacity samples, even at one a minute per window, are smaller than that.

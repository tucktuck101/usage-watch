# R10: Can omp hand over its limit readings each turn, without a fetch?

Date: 2026-10-01. Evidence: omp 18.4.x's embedded source (the Bun bundle
keeps source-path markers such as `packages/ai/src/auth/usage.ts`), read
with `strings`, plus `omp usage --help`. No omp command that fetches was
run, and no credential was read.

## Answer: yes, from response headers, for the login serving each session

omp calls the extension event **`after_provider_response`** on every model
HTTP response, with `{type, status, headers, requestId, metadata}`, and
`ctx.model` set. omp itself parses the same headers for its own limit
state:
- Anthropic: `anthropic-ratelimit-unified-5h`, `-7d`, `-7d_oi`.
- Codex: `x-codex-{primary,secondary}-{used-percent,window-minutes,reset-at}`.

An extension reading **only those response headers** gets a fresh limit
reading on every request, with no extra network call and no credential.
The headers belong to the response, not the request.

### A minimal extension (sketch only; nothing is written yet)

1. On `after_provider_response`, if `ctx.model.provider` is `anthropic` or
   `openai-codex`, keep only the rate-limit headers listed above. Never
   touch request headers or anything in the auth store's credentials.
2. Identify the serving login through omp's account-identity helper, and
   **hash it immediately**.
3. Write a small JSON snapshot atomically: provider, hashed account,
   window, used fraction, resets at, status, observed at.

### Limits

- **Only the login serving a session gets readings.** omp's other logins
  get none until omp next fetches their usage.
- **Don't read omp's usage cache through its public API.** `usage.report()`
  and `reports()` return cached data only while it's fresh (5 minutes, ±25%
  jitter). Otherwise they **fetch over the network** with omp's credentials.
  There is no public read that is guaranteed to stay cache-only.
- **`omp usage --json` can fetch, and can refresh an OAuth token.** Not to
  be used. `omp usage --history --json` reads the database only.
- **Where the extension runs:** inside omp's process, so it *could* reach
  omp's credentials. Keeping to the no-credentials rule (see the plan)
  rests on the extension's code, not on isolation. That's another reason
  it must stay tiny and reviewed.

## Why `usage_history` looks hourly

History rows are bucketed by the hour and written only when omp *fetches*
usage, never from headers. omp throttles its header ingest to once per 60
seconds unless a limit is exhausted.

## A simpler route: omp's usage cache (R10b, verified)

omp keeps each login's latest usage report in its database's `cache` table
(`key, value, expires_at`), under keys `usage_cache:report:…`. A read-only
query on 2026-10-01 found four entries: two Anthropic and two Codex. Each
held `value.provider`, `value.fetchedAt` and `value.limits[]` with the
report shape below (e.g. a 5-hour window with `usedFraction` and `status`).

Watching `fetchedAt` for 2.5 minutes while omp was working:

| Entry | Readings |
|---|---|
| Anthropic (serving) | 00:03:54, 00:05:28, 00:06:32, 00:07:07: about once a minute |
| Codex (one login) | 00:01:16, then 00:06:18 |
| Codex (other login) | 00:03:06, unchanged |
| Anthropic (second entry) | unchanged since 21:29 the previous evening, `exhausted`: a stale report, to be ignored by age |

So usage-watch can read omp's own limit readings **read-only, with no
extension, no network call and no credential**, about a minute old for a
login in use, and older for idle ones, which is fine because idle pools
don't move. Caveats: the format is internal to omp and may change between
versions, and entries carry identity metadata (email, account, org), which
is hashed on reading. Only the `value` of `usage_cache:report:*` keys is
read. Nothing else in the database is touched.

## Report shape

- **A report:** `{provider, fetchedAt, limits[], …}`.
- **Each limit:** `{id, label, scope{provider, accountId?, orgId?, modelId?, windowId?, …}, window{id, durationMs?, resetsAt?}, amount{used?, limit?, usedFraction?, remainingFraction?, unit}, status: ok|warning|exhausted|unknown}`.

Identity fields appear in `scope`, `metadata` and history `accountKey`, and
all of them are hashed on arrival (D5).

## Consequences for the plan

- **C1 for omp's logins, in order:**
  1. the `usage_cache` table, read-only (verified, about a minute fresh);
  2. an opt-in omp extension reading response headers, only if the cache
     format changes;
  3. `usage_history` (hourly) as the fallback.
- **The same header source matters beyond omp.** Claude Code's status line
  gets its values from the same Anthropic headers (R9), so both routes show
  one underlying truth.
- **Team plans:** whether the Anthropic headers carry values for Team
  plans needs one live observation.

## Open

2. Do Anthropic's unified rate-limit headers carry values on Team plans?
3. Is omp's account-identity helper free of side effects?

# D1: The model

Status: draft, revised after review (2026-10-01). Rests on
[R1](../research/R1-native-otel.md), [R2](../research/R2-local-token-records.md),
[R3](../research/R3-capacity-sources.md), [R9](../research/R9-credential-free-limits.md),
[R10](../research/R10-omp-extension-limits.md).

Scope markers used in every design document:
- **[F]** must be settled before the foundation build (F1, F2);
- **[X]** needs only an extension point now;
- **[L]** belongs to a later phase and is sketched only.

## Provenance, at two levels [F]

1. **Measured facts carry the record's own provenance.** Token counts,
   capacity percentages and cost amounts carry:
   - `source`, the collector, e.g. `claude.transcript`, `omp.usage_cache`;
   - `confidence`:
     - `authoritative`: the harness or provider stated it;
     - `observed`: read from a copy another tool kept;
     - `inferred`: deduced, e.g. a state read from a screen.
2. **Enrichment carries its own provenance**, separately, as
   **attributions**. Project, account, branch, pane and the other enriched
   dimensions are never stored as plain columns on a usage record. Each is
   an attribution with its own method and confidence (D2).

**Authoritative token counts never make an inferred attribution look
authoritative.** A query can therefore say "94% of usage attributed to an
account authoritatively, 4% inferred, 2% unattributed".

## Units and conventions [F]

- Times: UTC milliseconds, integers.
- Money: USD micro-units, integers (`…_usd_micros`).
- Percentages: 0–100, floats.
- Tokens: integers.
- **Absent is null, never 0.**
- Identities are hashed keys (D5, D7). Raw emails and IDs are never stored.

## Token fields [F]

Explicit names, so no field means something surprising:

| Field | Meaning |
|---|---|
| `uncached_input_tokens` | input not served from cache |
| `cache_read_input_tokens` | input served from cache |
| `cache_write_input_tokens` | input written to cache |
| `output_tokens` | all output, **including** reasoning |
| `reasoning_output_tokens` | the reasoning part of `output_tokens`, a subset, never added to it |
| **`total_input_tokens`** | **derived**: `uncached + cache_read + cache_write`. Never stored, always computed |

- Each collector maps its source's fields into these, with the rule tested
  against a recorded sample (D4 holds the per-source mapping).
- Where a source reports reasoning *separately* from output, the collector
  adds it into `output_tokens` so the subset rule holds. Whether each
  source does this is verified per source, not assumed.
- Each observation also keeps its source's **native numeric token fields**,
  as a small JSON object of numbers only, so normalisation can be re-run
  if a harness changes its meaning. No text is kept.

## Capacity evidence: three kinds, never confused [F]

| Kind | What it proves | Used for |
|---|---|---|
| **Anchor** | Current capacity, as of `observed_at`: a real reading from a harness (status line, omp's cache, Codex's `rate_limits`, Claude's cache) | views, alerts, **and nudges** when fresh enough (D8) |
| **Recovery signal** | That a *previously observed* blocking condition has expired: an anchor's `resets_at` has passed, or a harness notice says the limit reset. It does **not** prove capacity is available now, because usage elsewhere may have happened since | nudges, under the conditions in D8 |
| **Estimate** | A forecast from the usage stream between anchors (V4) | views and alerts **only, never a nudge** |

- **Estimates are computed at query time and never stored.** A change to
  the estimator can't leave stale numbers behind.
- Whenever one is shown, it comes with its estimator version, the anchor
  it starts from, the calibration's age, and an error range where one can
  be computed. [L]

## Records

### Capacity sample (anchors only) [F]

| Field | Meaning |
|---|---|
| `account` | canonical account key (D7) |
| `window` | `session` (5 h), `weekly` (7 d), `weekly:<model>`, or `other:<name>` |
| `window_seconds` | when known. Codex's `primary`/`secondary` map to `session`/`weekly` by this value (18000, 604800), not by name |
| `used_pct` | 0–100 |
| `resets_at` | when the window resets |
| `status` | `ok`, `warning`, `exhausted` or `unknown` |
| `source`, `confidence`, `observed_at` | provenance |

### Limit event [F]

A harness's own statement that a limit was **hit** or has **reset**: Claude
transcript notices, screen messages. Fields: `account` (via attribution),
`window` if stated, `kind` (`hit` or `reset`), `resets_at` if stated, and
provenance. A `reset` event is a recovery signal.

### Usage observation [F]

What **one source** said about **one request**:
- `source`, `source_request_key`, `parser_version`, `observed_at`;
- `harness`, `provider`, `model`, `session_id`;
- the token fields above, plus `native` (the source's numeric fields);
- `auxiliary`: set for a harness's own side calls, such as omp's judgment
  model.

**Within one source, repeats collapse before they become an observation.**
Claude writes a request once per content block while streaming, with
`output_tokens` rising. Those are partial snapshots of one fact, not
disagreeing evidence. The source's counting rule (D6) keeps the final
snapshot as the observation.

### Usage event (canonical) [F]

What usage-watch **believes** one request consumed, reconciled across
sources:
- `usage_id`, `observation_count`, `chosen_observation`;
- the reconciled token fields and `auxiliary`;
- `disagreement`: null when every observation agreed, otherwise the
  largest per-field difference, e.g. transcript 12,400 versus OTel 12,402;
- `billing_route`: `api_key`, `subscription` or `unknown`. From
  evidence only: Codex OTel `auth_mode` is known. Claude and omp are open,
  see the plan.

Reconciliation rules are versioned. Re-running them over stored
observations rebuilds usage events without losing evidence.

### Attribution [F]

One enriched dimension of one subject:
- `subject`: a session or a usage event;
- `dimension`: `account`, `checkout`, `repository`, `project`, `branch`,
  `worktree`, `pane`, `role`, `task`;
- `value`, `method`, `confidence`;
- `validity`: `historical`, `live` or `time_bounded` (D2).

Most dimensions attach to the **session**, and usage events inherit them.
A usage event carries its own attribution only where it differs, e.g. a
branch recorded per request.

### Cost event [F: shape; L: pricing]

| Field | Meaning |
|---|---|
| `scope_kind`, `scope_id` | `request` (a `usage_id`) or `session` (a `session_id`) |
| `source` | who produced the figure |
| `basis` | `actual_billed` (a real bill), `harness_estimate` (the harness's own figure), or `list_price` (usage-watch: tokens × a public price table) |
| `price_version` | for `list_price`: the table's version and entry |
| `cost_usd_micros` | the amount |

**Identity:** `(scope_kind, scope_id, source, basis, price_version)`.
Revised estimates sit alongside older ones. Views use the newest
`price_version` unless asked otherwise.

**What a figure is called depends on its basis and the request's billing
route:**

| Basis | Billing route | Shown as |
|---|---|---|
| `actual_billed` | any | **Spend**. The only thing ever called spend |
| `list_price` or `harness_estimate` | `api_key` | Estimated bill |
| `list_price` or `harness_estimate` | `subscription` | API list-price equivalent |
| `list_price` or `harness_estimate` | `unknown` | List-price value, billing route unknown |

**No double counting:** a session's cost is the sum of its request-level
costs when any exist. A session-level figure is used only for sessions with
no request-level costs, and the two are never added.

### Agent-state sample [F]

`pane`, `harness`, `session_id`, `state` (`busy`, `idle`, `stalled`,
`resuming`, `typing`, `unknown`), `model`, `reset_hint`, and `error_key`.
Always `inferred`. Short retention (D3).

### Context event [X]

`kind` (`task_start`, `task_end`, `role`, `handoff`, `retry`), `session_id`,
`pane`, and user-supplied `task`, `issue`, `pr`, `role`, `work_kind`. This
is developer-context data (D5). Defined now so phase 8 adds rows, not a new
model.

## Entities [F]

- **Session:**
  - `session_id`, `harness`, `parent_session_id`;
  - `started_at`, `last_seen_at`, `live` (seen in a running process
    recently);
  - its attributions.
- **Checkout and repository:** D2.
- **Account and aliases:** D7.
- **Pane:** a live tmux fact only, never an entity.

## Invariants [F]

These become tests, and later drift detectors when a harness changes its
formats.

1. **Totals partition.** Total usage = attributed + unattributed, for every
   dimension.
2. **Grouping never changes the total.** Changing a grouping dimension
   leaves the grand total unchanged, unless a view applies a named filter.
3. **Auxiliary is inside the total.** Provider total = primary + auxiliary.
   Views filter auxiliary only when they say so.
4. **Input composition.** `total_input_tokens = uncached + cache_read +
   cache_write`, and `reasoning_output_tokens ≤ output_tokens`.
5. **One canonical event per request.** The number of usage events equals
   the number of distinct requests, whatever the number of observations.
6. **Cost bases never mix.** No sum crosses bases. Only `actual_billed` is
   ever labelled spend.
7. **No estimate authorises a nudge** (D8).
8. **Nothing unknown becomes zero.** A null stays null through every
   aggregation, and is reported as unknown.

## Not in the model

Prompt and response content, tool inputs and outputs, file contents, raw
OTLP bodies, credentials of any kind (D5).

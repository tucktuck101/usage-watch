# R3: What account-level capacity sources exist per provider?

Date: 2026-09-30. Status: answered for Claude and Codex subscriptions.

Capacity means the quota pools of a subscription: how much of each window is
left, and when it resets. Token usage and cost are separate questions (R2,
R5).

## Sources found

| Source | Provider | Gives | Costs a request? | Freshness | Platforms | Evidence |
|---|---|---|---|---|---|---|
| Usage endpoint, with the harness's own login | Claude | Every window: `five_hour`, `seven_day`, model and product windows, `limits[]`, spend and overage state, and a weekly breakdown | Yes, against a per-token allowance | Live | Any (the token lives in the Keychain on macOS, and in `~/.claude/.credentials.json` elsewhere) | [0.1](0.1-endpoint-check.md) |
| Usage endpoint, with the harness's own login | Codex | 5-hour and weekly windows (`used_percent`, `reset_at`), limit-reached flags, model availability, credits | Yes | Live | Any (`~/.codex/auth.json`) | [0.1](0.1-endpoint-check.md) |
| omp's `usage_history` table (`~/.omp/agent/agent.db`) | Claude and Codex, for every login omp holds | Per account and window: `used_fraction`, `resets_at` (Unix ms), `status`, window label. Seen: `anthropic:5h`, `anthropic:7d`, `anthropic:7d:fable`, `openai-codex:primary`, `openai-codex:secondary` | No: it reads what omp already fetched | Irregular. Mostly about hourly, sometimes minutes apart (on 2026-09-30: 17:55, 18:58, 19:56, 20:50, 21:59, 22:54, 23:00) | Any | Read-only query of the table's schema, limit IDs, counts and timestamps |
| Claude Desktop's login | Claude, including team organizations | The same as the Claude endpoint, per organization | Yes | Live | **macOS only**: its token cache is encrypted with a key from the Keychain item "Claude Safe Storage" | OpenUsage source, `ClaudeDesktopAuthStore.swift` |
| OpenUsage CLI | Several | A normalised form of the above | Its own polling | Cached for five minutes | macOS only | **Removed as a dependency** (plan decision, 2026-09-30) |
| The harness's own limit message on screen | Claude, Codex | A reset time only, e.g. "try again at 10:29 PM" or "resets 3pm" | No | Only at the moment of a stall | Any | The adapters' fixtures |

## Findings

- **A direct live read works for Claude and Codex** with the logins the
  harnesses already hold, as long as the token is valid and not throttled
  ([0.1](0.1-endpoint-check.md)).
- **omp's `usage_history` is the only source that costs nothing and covers
  every account omp holds**, including a team plan whose token was
  throttled in 0.1. Its drawback is freshness: an hour-old reading can be
  far off. On 2026-09-30 the team plan's 5-hour pool dropped about 29
  points in an hour.
- **The two sources agree on the numbers** where they overlap. The
  team-plan 5-hour pool read about 29% used from OpenUsage at 22:09, and
  omp recorded 34% used at 23:00.
- **Every live source spends a per-token allowance**, which omp, other
  tools and usage-watch may all be drawing on (0.1). Passive sources come
  first, and live polling stays within a budget (plan principles).
- **A reset time on screen** is available at no cost, exactly when it
  matters most (a stall). It is a useful fallback for deciding when to look
  again, though not proof of capacity.

## Consequences

- C1 reads, in order: omp's `usage_history` (no cost), then the direct
  endpoint for each login within the poll budget, then the on-screen reset
  hint to schedule the next look.
- Every reading carries its source, when it was taken, and a confidence:
  `authoritative` for a live endpoint read, `observed` for omp's recorded
  copy.
- The account identities in omp's table (`account_key`, `email`,
  `account_id`) are personal. They must be hashed before storing, and never
  shown (D5, D7).

## Open

- The team-plan direct read after a quiet period (see 0.1).
- Whether omp exposes a way to trigger its own usage refresh. Using that
  would mean usage-watch adds no extra polling.

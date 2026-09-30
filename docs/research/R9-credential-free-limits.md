# R9: Can Claude plan limits be read without a Claude credential?

Date: 2026-09-30. Evidence:
- Anthropic's docs, verified directly;
- the Claude Code 2.1.285 binary's strings;
- key names only from local state files;
- a survey of how other tools responded to Anthropic's terms
  ([R8](R8-omp-authentication.md)).

No credential was read.

## Answer: yes, from Claude Code's own status line

Claude Code passes a `rate_limits` object on stdin to the user's
configured `statusLine` command. Verified on the
[status line docs](https://code.claude.com/docs/en/statusline), and
present in the binary:

| Field | Meaning |
|---|---|
| `rate_limits.five_hour.used_percentage`, `rate_limits.seven_day.used_percentage` | Percentage of the 5-hour or 7-day limit used, 0–100 |
| `rate_limits.five_hour.resets_at`, `rate_limits.seven_day.resets_at` | Unix epoch seconds when the window resets |
| `rate_limits.spend_limit.*` | Only behind a Claude apps gateway with a spend limit |

- **Who gets it:** "only for claude.ai Pro and Max subscribers, or behind a
  Claude apps gateway with spend limits, and only after the first API
  response" (docs). Each window may be absent on its own. Team and
  Enterprise are **not** listed. A secondary source reports a Team fix in
  v2.1.225, unconfirmed.
- **When it updates:** on events (debounced at 300 ms), when a window
  reaches its `resets_at`, and every N seconds if `refreshInterval` is set
  (docs and binary). The values come from the response headers of Claude
  Code's own API calls (inferred from the binary).
- **No credential involved:** Claude Code uses its own login, and a status
  line script receives only these numbers. This is how
  [claude-hud](https://github.com/jarrodwatts/claude-hud) works, and it
  describes itself as making no network requests and scraping no
  credentials.
- **This machine already has a status line script that reads these
  fields.** It prints them but keeps nothing.

## Other credential-free sources

| Source | Gives | Freshness | Notes |
|---|---|---|---|
| `~/.claude.json` `.cachedUsageUtilization` | Richer: `five_hour`, `seven_day`, per-model weekly windows, extra usage, `fetchedAtMs` | Only as fresh as the last time the user opened `/usage`. Here it was about 2.9 h old. Claude Code itself ignores it after 1 h | Written by Claude Code, not by us. Holds an account UUID, which must not be stored or shown |
| Transcripts: `error:"rate_limit"` records and system notices | Limit **hit** and **reset** events with a reset time ("You've hit your session limit · resets …", "Usage limit reached · continuing automatically at …") | When it happens | No percentages, but exact stall and reset moments |
| Local token accounting, as in [ccusage](https://ccusage.com/guide/blocks-reports) | Tokens, cost, 5-hour block boundaries, burn rate | Live | Without a known limit, only a projection. Anchored to the last known `used_percentage`, it becomes a reasonable estimate of time until the limit |
| omp's `usage_history` / `omp usage --json` | Every account omp holds, including team plans | About hourly | Covers what the status line doesn't (Team) |
| `/usage` driven in a pane | The full usage screen | On demand | Typing into a user's session is brittle, and whether it's allowed is unclear. Not recommended |

**Nothing else carries limits.** Hook inputs and Claude Code's OTel metrics
carry no limit data, and Claude Code writes no rate-limit headers to disk
(verified; Anthropic
[closed a request](https://github.com/anthropics/claude-code/issues/55333)
to persist them).

## How others handled the block

| Tool | Claude limits from | Credential? |
|---|---|---|
| claude-hud | status line `rate_limits`, plus a local snapshot | **no** |
| ccusage | local transcripts; block limit is a heuristic ("highest previous block") | **no** |
| Claude-Code-Usage-Monitor | local transcripts, fixed per-plan guesses or the 90th percentile of history | **no** (low accuracy) |
| ccstatusline | status line first, API call as fallback | fallback yes |
| OpenUsage | OAuth usage endpoint; only local spend is credential-free | yes |
| CodexBar | OAuth endpoint, cookies, or `/usage` in a pty; local logs for cost | mostly yes |

Blocked harnesses (OpenCode, Goose and others) moved to Console API keys
or dropped Claude sign-in. Sources for each are in the research notes
behind this summary: [gigazine](https://gigazine.net/gsc_news/en/20260220-anthropic-third-party-block),
[moltis](https://docs.moltis.org/anthropic-oauth.html).

## Codex

- OpenAI has publicly welcomed ChatGPT-plan use in third-party tools, but
  through statements, not a terms clause
  ([secondary](https://manifest.build/blog/chatgpt-plus-tokens-third-party-harnesses/)).
- Credential-free sources exist anyway:
  - `rate_limits` on `token_count` events in Codex's session files
    ([R2](R2-local-token-records.md));
  - Codex's own `app-server`, which answers `account/rateLimits/read` using
    Codex's own login. That's how
    [CodexBar](https://github.com/steipete/CodexBar/blob/main/docs/codex.md)
    reads it.

## Consequences for the plan

1. **C1, Claude:** a **status line tap**.
   - What it is: `usage-watch init` offers to wrap the user's existing
     `statusLine` command. Output passes through unchanged, and the
     `rate_limits` it receives are written atomically, with a timestamp and
     session ID, to a local file that usage-watch reads.
   - Two conditions: it's opt-in, and it's reversible (`init` shows the
     change and can undo it).
   - Several concurrent sessions: keep the newest reading per window.
   - Gaps to fill:
     - `cachedUsageUtilization`, honouring its age;
     - transcript limit events, for exact stall and reset times;
     - omp's record, for Team plans.
2. **C1, Codex:** read `rate_limits` from its session files, with no
   network call. Codex's `app-server` is an option for a live read.
3. **No credential of any kind is read by usage-watch.** That removes the
   R8 conflict entirely, for both providers.
4. **Views:** the status line tap plus local token accounting gives burn
   rate and "time until limit" (V3), anchored to real percentages rather
   than guessed plan limits.

## Open

1. Does Team or Enterprise get `rate_limits` in the status line? The docs
   say Pro and Max only.
2. Max plans reportedly sometimes lack the field (a closed "not planned"
   report). Check on a real account.
3. An idle session gives no fresh reading. Does `refreshInterval` help
   while idle, or does it only re-render the last value?
4. Is wrapping the user's status line command something to do
   automatically, or only by explicit instruction? (Recommended: explicit,
   with a preview and an undo.)

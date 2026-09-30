# D7: Accounts

Status: draft. Rests on [R7](../research/R7-logins-and-accounts.md),
[R10](../research/R10-omp-extension-limits.md) and
[R1's live capture](../research/R1-native-otel.md#live-capture-2026-10-01).

## Identity

An account is the provider's own stable ID, hashed (D5):

| Provider | Raw identity | Where usage-watch reads it (never from a credential) |
|---|---|---|
| Anthropic | `accountUuid` + `organizationUuid` | `~/.claude.json` `oauthAccount`; Claude OTel `user.account_uuid` + `organization.id`; transcript owner fields; omp usage-cache metadata (`accountId`, `orgId`) |
| OpenAI (Codex) | `account_id` | Codex OTel `user.account_id`; omp usage-cache metadata (`accountId`) |

- **The organization is part of an Anthropic identity.** A personal and a
  team org under one person are two accounts, each with its own pools.
- **Linking across tools:** omp's `identity_key` (`email:…|org:…`) doesn't
  equal the provider's IDs, so omp accounts are linked through the
  usage-cache metadata's `accountId`/`orgId`. That these equal Claude's
  `accountUuid`/`organizationUuid` is **inferred**, and is checked the
  first time both are seen. A mismatch keeps them as two accounts and
  `doctor` reports it.

## The registry

The `accounts` table holds:
- `key`, the hashed identity;
- `provider`;
- `label`, the user's name for it, such as "team" or "personal";
- `plan`, when a source states it;
- `first_seen` and `last_seen`;
- `sources`, the collectors that have seen it;
- `removed_at`, a tombstone.

- **Labels:** `usage-watch accounts` lists accounts, and
  `usage-watch accounts label <key-prefix> <name>` names one. Unlabelled
  accounts display as `<provider> account <first 6 of key>`.
- **Tombstones:** an account unseen for 90 days is marked `removed_at` and
  hidden from views, not deleted. Its history remains. Seeing it again
  clears the mark.

## Attribution

D2 covers how a session is joined to an account. The effect on capacity:
- A stalled pane's pool is the account its session is attributed to.
- If that's unknown, the pane's harness and model family pick the pool,
  the way today's config does.
- If that's ambiguous, the stall waits and `doctor` says why, as today.

## Poll budget

No collector makes a network call (D6), so there is currently nothing to
budget. The rule stays in the plan for any future source that does: poll
each account at most every few minutes, with jitter, and back off on 429.

## Replaces today's config

`[accounts.<harness>]` in the config file becomes a fallback only, for
harnesses whose sessions can't be attributed. `init` stops asking about it
once attribution works.

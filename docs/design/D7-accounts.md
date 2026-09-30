# D7: Accounts

Status: draft, revised after review (2026-10-01). Rests on
[R7](../research/R7-logins-and-accounts.md), [R10](../research/R10-omp-extension-limits.md),
[R1's live capture](../research/R1-native-otel.md#live-capture-2026-10-01).
Scope markers as in [D1](D1-model.md).

## Canonical accounts and aliases [F]

An account is one quota-holding identity at a provider. For Anthropic that
means the account **plus the organization**: a personal and a team org are
two accounts.

Each source identifies accounts its own way, so the registry has two
layers:

| Table | Holds |
|---|---|
| `accounts` | `account_key` (canonical), `provider`, `label` (the user's name), `plan` (when stated), `first_seen`, `last_seen`, `removed_at` |
| `account_aliases` | `alias` (a hashed source identifier), `alias_kind` (what it is), `account_key`, `asserted_by` (source), `evidence`, `confidence`, `created_at`, `revoked_at` |

**Alias kinds and where they come from** (never from a credential):

| Alias kind | Source |
|---|---|
| `anthropic.account_org` (`accountUuid` + `organizationUuid`) | `~/.claude.json` `oauthAccount`; Claude OTel; transcript owner fields |
| `openai.account` (`account_id`) | Codex OTel |
| `omp.identity_key` | omp's `auth_credentials.identity_key` column (only that column) |
| `omp.report_account` (`accountId` + `orgId`) | omp's usage-cache metadata |

## Merging [F]

1. **A new alias** creates a new canonical account unless merge evidence
   links it to an existing one.
2. **Evidence that allows a merge,** strongest first:
   - `same_identifier`: the same provider ID reaches us through two routes,
     e.g. the Claude OTel account and org equal `~/.claude.json`'s, or omp's
     report account and org equal them. **Authoritative.**
   - `co_reported`: one source reports both identifiers together, e.g. an
     omp usage report whose `identity_key` and `accountId` sit in the same
     entry. **Authoritative.**
   - `user`: the user merges two accounts with
     `usage-watch accounts merge`. Recorded as such.
3. **No merge on weaker grounds**: not on matching labels, plans or
   timing.
4. **Undoing:** a merge is a set of alias rows, so it's undone by setting
   `revoked_at` on the aliases it added
   (`usage-watch accounts unmerge <alias>`). Attributions that relied on
   it are recomputed. Nothing depends on which account was seen first.
5. **Conflicts:** when authoritative evidence points one alias at two
   accounts, neither merge is applied, and `doctor` reports it.

Whether omp's report `accountId` equals Claude's `accountUuid` is
**inferred**. The `same_identifier` rule tests it the first time both are
seen, rather than assuming it.

## Labels and tombstones [F: fields; X: commands]

- `usage-watch accounts` lists accounts, and `accounts label <key> <name>`
  names one.
- An account unseen for 90 days gets `removed_at` and is hidden, not
  deleted. Seeing it again clears the mark.

## Attribution for nudging [F]

- A stalled pane's pool is its session's attributed account (D2).
- Whether that attribution is certain enough to act on is decided in D8.

## Replaces today's config [F]

`[accounts.<harness>]` in the config file becomes a fallback only, used
when a session's account can't be attributed. D8 treats that fallback as
`inferred`.

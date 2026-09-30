# D7: Accounts

Status: draft, revised after second review (2026-10-01). Rests on
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
| `account_aliases` | `alias` (a hashed source identifier), `alias_kind` (what it is), `account_key`, `asserted_by` (source), `evidence`, `verified_by` (for a `co_reported` merge), `confidence`, `created_at`, `revoked_at` |

**Alias kinds and where they come from** (never from a credential):

| Alias kind | Source |
|---|---|
| `anthropic.account_org` (`accountUuid` + `organizationUuid`) | `~/.claude.json` `oauthAccount`; Claude OTel; transcript owner fields |
| `openai.account` (`account_id`) | Codex OTel |
| `omp.identity_key` | omp's `auth_credentials.identity_key` column (only that column) |
| `omp.report_account` (`accountId` + `orgId`) | omp's usage-cache metadata |

## Merging [F]

A merge is applied **only on evidence of verified semantic equivalence**.

1. **A new alias** creates a new canonical account unless merge evidence
   links it to an existing one.
2. **Evidence that allows a merge:**
   - `same_identifier`: the same identifier type reaches us through two
     routes, e.g. the Claude OTel account and org equal `~/.claude.json`'s.
   - `co_reported`: one source reports both identifiers together, **and**
     it's documented or verified that they denote the same account. An
     omp usage report whose `identity_key` and `accountId` sit in the same
     entry merges only once that is documented or verified. The merge
     records what verified it in `verified_by`: `doc:<reference>` or
     `check:<name>`.
   - `user`: the user merges two accounts with
     `usage-watch accounts merge`. Recorded as such.
3. **An inferred equivalence does not merge.** omp's report `accountId`
   and Claude's `accountUuid` are different identifier types, and their
   equality is only inferred. When they match, the two stay separate
   accounts, and `doctor` shows them as a **merge candidate**, with the
   evidence, until the equivalence is verified or the user merges them.
4. **No merge, and no candidate, on weaker grounds**: not on matching
   labels, plans or timing.
5. **Undoing:** a merge is a set of alias rows, so it's undone by setting
   `revoked_at` on the aliases it added
   (`usage-watch accounts unmerge <alias>`). Effective attributions that
   relied on it are recomputed (D2). Nothing depends on which account was
   seen first.
6. **Conflicts:** when merge evidence points one alias at two accounts,
   neither merge is applied, and `doctor` reports it.

## Account discovery [F]

Each harness declares whether its **account discovery is complete**:

| Value | Meaning |
|---|---|
| `complete` | every login the harness can use is enumerated from a source usage-watch reads, so the registry's accounts for that harness are the full set |
| `partial` | usage-watch sees only some logins, so the registry's accounts for that harness are a lower bound |

| Harness | Discovery | Why |
|---|---|---|
| omp | `complete` | `auth_credentials` lists every login, and its `identity_key` column is read (R7) |
| Codex | `partial` | accounts appear only through OTel. The credential file, the only full record, isn't opened |
| Claude | `partial` | each home's `.claude.json` shows only its current login, and only homes seen in use are known (R7) |

- The value is declared per harness in the collector's code, with the
  evidence, and shown by `doctor`.
- A harness whose identity source failed on its last pass is treated as
  `partial` until it succeeds again.
- **An "exactly one account" fallback is only meaningful where discovery
  is `complete`.** With `partial` discovery, one account in the registry
  doesn't mean one account exists. D8 applies this.

## Labels and tombstones [F: fields; X: commands]

- `usage-watch accounts` lists accounts, and `accounts label <key> <name>`
  names one.
- An account unseen for 90 days gets `removed_at` and is hidden, not
  deleted. Seeing it again clears the mark.

## Attribution for nudging [F]

- A stalled pane's pool is its session's **effective** account
  attribution (D2), and capacity samples and limit events are matched to
  it by their own effective account.
- An `ambiguous` or `unattributed` account can't be nudged on.
- Whether an `attributed` account is certain enough to act on is decided
  in D8.

## Replaces today's config [F]

`[accounts.<harness>]` in the config file becomes a fallback only: it is
attribution evidence with method `config` and confidence `inferred` (D2),
so it decides a session's effective account only where no
higher-confidence evidence exists. D8 applies its rules for `inferred`
accounts to it.

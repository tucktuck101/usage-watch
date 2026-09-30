# D7: Accounts

Status: draft, revised after third review (2026-10-01). Rests on
[R7](../research/R7-logins-and-accounts.md), [R10](../research/R10-omp-extension-limits.md),
[R1's live capture](../research/R1-native-otel.md#live-capture-2026-10-01).
Scope markers as in [D1](D1-model.md).

## Canonical accounts and aliases [F]

An account is one quota-holding identity at a provider. For Anthropic that
means the account **plus the organization**: a personal and a team org are
two accounts.

Each source identifies accounts its own way, so the registry has three
tables:

| Table | Holds | Identity |
|---|---|---|
| `accounts` | `account_key`, `provider`, `label` (the user's name), `plan` (when stated), `first_seen`, `last_seen`, `removed_at` | `account_key` |
| `account_aliases` | one source's assertion that an alias denotes an account: `alias_kind`, `alias_hash` (the hashed source identifier, D5), `account_key`, `asserted_by` (the source), `evidence`, `confidence`, `verified_by`, `first_seen`, `last_confirmed`, `revoked_at` | `(alias_kind, alias_hash, account_key, asserted_by)`, all NOT NULL |
| `account_merges` | one merge of two accounts: `merge_id`, `from_key`, `into_key`, `evidence`, `verified_by`, `created_at`, `revoked_at` | `merge_id` |

- **`account_key` is a random opaque local ID** (128-bit, hex), generated
  when the account is created. It's never derived from an alias, so which
  alias was seen first doesn't shape the key.
- **Several sources asserting the same alias are separate rows**, one per
  `asserted_by`. A source seeing it again updates its own row's
  `last_confirmed`.
- **Conflicting assertions are separate rows too:** the same alias
  asserted against different accounts gives rows differing in
  `account_key`. Nothing overwrites another source's row.

**Alias kinds and where they come from** (never from a credential):

| Alias kind | Source |
|---|---|
| `anthropic.account_org` (`accountUuid` + `organizationUuid`) | `~/.claude.json` `oauthAccount`; Claude OTel; transcript owner fields |
| `openai.account` (`account_id`) | Codex OTel |
| `omp.identity_key` | omp's `auth_credentials.identity_key` column (only that column) |
| `omp.report_account` (`accountId` + `orgId`) | omp's usage-cache metadata |

**Alias `evidence`:**

| Value | The row asserts |
|---|---|
| `reported` | the source reported this alias on its own. The row is written against the account the alias's existing unrevoked `reported` rows use; if it has none, a new account is created for it |
| `co_reported` | the source reported this alias in the same entry as another alias, and the row is written against that other alias's account. `verified_by` is set once the pairing is documented or verified (below). This row is merge evidence, never identification |

**The canonical account** of any `account_key` is the result of following
unrevoked merges from it (`from_key` to `into_key`) until an account with
no unrevoked outgoing merge. **An alias identifies** the canonical account
its unrevoked `reported` rows resolve to. If they resolve to more than one,
the alias is **in conflict**: it identifies no account, attribution
evidence resting on it resolves to `ambiguous` (D1), and `doctor` reports
it.

## Merging [F]

A merge is a row in `account_merges`, created **only on evidence of
verified semantic equivalence, or by the user**. Aliases never move: a
merge joins accounts, and no alias row is rewritten.

1. **A new alias** creates a new canonical account unless it already has
   `reported` rows.
2. **Evidence that establishes equivalence:**
   - `same_identifier`: the same identifier type reaches us through two
     routes, e.g. the Claude OTel account and org equal `~/.claude.json`'s.
     Both are the same alias, so this is a second `reported` row on the
     same account, and needs no merge.
   - `co_reported`: one source reports both identifiers together, **and**
     it's documented or verified that they denote the same account. An
     omp usage report whose `identity_key` and `accountId` sit in the same
     entry merges only once that is documented or verified. `verified_by`
     records what verified it, on the `co_reported` alias row and on the
     merge: `doc:<reference>` or `check:<name>`. The merge joins the two
     aliases' canonical accounts.
   - `user`: the user merges two accounts with
     `usage-watch accounts merge <key> <key>`. Recorded with evidence
     `user`.
3. **An inferred equivalence does not merge.** omp's report `accountId`
   and Claude's `accountUuid` are different identifier types, and their
   equality is only inferred. When they match, the two stay separate
   accounts, and `doctor` shows them as a **merge candidate**, with the
   evidence, until the equivalence is verified or the user merges them. An
   unverified `co_reported` row is shown the same way.
4. **No merge, and no candidate, on weaker grounds**: not on matching
   labels, plans or timing.
5. **Shape of a merge:** `from_key` and `into_key` are both canonical when
   the merge is created, so an account has at most one unrevoked outgoing
   merge, and a merge that would form a cycle is refused. For a merge from
   evidence, `into_key` is the smaller of the two keys; the keys are
   random, so this favours neither account. For a user merge, the user
   names the direction.
6. **Conflicts:** when merge evidence points one alias at two accounts
   (e.g. one `accountId` co-reported with two different `identity_key`s),
   the `co_reported` rows are kept, neither merge is applied, and `doctor`
   reports the conflict. The same applies to an alias in conflict.

## Undoing a merge [F]

- `usage-watch accounts unmerge <merge_id>` sets the merge's `revoked_at`.
  Nothing else changes: aliases stay where they were asserted, so each
  account's canonical form returns to what it was without the merge.
- Effective attributions that relied on it are recomputed (D2).
- A merge the user revoked isn't created again from the evidence it
  rested on. Only new evidence or the user can merge those accounts again.
- Nothing depends on which account was seen first.

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
  doesn't mean one account exists. Accounts are counted as canonical
  accounts. D8 applies this.

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

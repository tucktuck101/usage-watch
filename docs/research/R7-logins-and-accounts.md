# R7: Where does each harness keep its logins, and can a pane be tied to an account?

Date: 2026-09-30. Evidence: OpenUsage source (MIT, commit `ab3e87b`; files
cited below), existence checks on this machine (paths, and Keychain items
by service name, never read), key names only from credential stores, and
the environment of live processes (only `CLAUDE_CONFIG_DIR` and
`CODEX_HOME` extracted). No secret, email or ID was read into the record.

## Where logins live

**Claude Code**
- **Home:** `CLAUDE_CONFIG_DIR`, else `~/.claude`. Credentials are in
  `<home>/.credentials.json`, or in the macOS Keychain under service
  `Claude Code-credentials`, which OpenUsage tries first. A custom home
  adds `-<first 8 hex of sha256(dir)>` to the service name, falling back
  to the unsuffixed one (`ClaudeAuthStore.swift` 62-63, 340-375, 447-451).
- **Identity, with no network call:** the state file next to the home
  (`~/.claude.json` for the default home, `<dir>/.claude.json` for a custom
  one). Key `oauthAccount.{accountUuid, organizationUuid}` gives an
  identity of `accountUuid|orgUuid`, lowercased
  (`DefaultAccountObserver.swift` 45-109). The organization is part of the
  key on purpose: a personal and a team org are two accounts.
- **Confirming a token:** `GET /api/oauth/profile` returns `account.uuid`
  and `organization.{uuid, organization_type, rate_limit_tier}`
  (`ClaudeUsageClient.swift` 32-55, 123-160).
- **Other stores:** Claude Swap (`~/.claude-swap-backup/sequence.json`,
  same identity key) and Claude Desktop (macOS only; Keychain
  `Claude Safe Storage` plus `config.json`).
- **Transcripts sometimes name their owner:** some lines carry
  `ownerAccountUuid`/`ownerOrganizationUuid`
  (`ClaudeSessionIdentity.swift` 11-47). They were present in 107 of 189
  transcripts here, but not the newest.

**Codex CLI**
- **Homes:** `CODEX_HOME`, else `~/.config/codex`, then `~/.codex`, plus
  sibling `~/.codex-*` and `~/.config/codex-*` homes, deduplicated after
  normalising (`CodexHomeScanner.swift` 54-129). Each home has an
  `auth.json` (keys: `OPENAI_API_KEY`, `last_refresh`,
  `tokens.{access_token, account_id, id_token, refresh_token}`).
- **Other stores:** Keychain service `Codex Auth`, account
  `cli|<16 hex of sha256(realpath(home))>`; the codex-swap registry
  (`accounts.json`).
- **Identity:** `tokens.account_id`, or the id_token claim
  `https://api.openai.com/auth`.`chatgpt_account_id`. If the two disagree,
  the login is rejected (`CodexSwapAccount.swift` 4-37). If a Keychain
  item exists, OpenUsage refuses to guess the default login's account.

**omp**
- **Logins:** `~/.omp/agent/agent.db`, table `auth_credentials`, several
  logins per provider. OAuth rows hold `access, refresh, expires,
  accountId, email, orgId, orgName, authorizedAt`, with an
  `identity_key` of the form `email:…`. Here: 1 Anthropic, 2 Codex,
  1 OpenRouter API key.
- **Choosing a login:** omp picks among logins itself: by usage headroom,
  sticky per session, backing off per login. `auth_credential_blocks`
  (`blocked_until_ms`, by scope) records back-offs. This comes from
  omp's changelog and the [upstream README](https://github.com/can1357/oh-my-pi)
  (inferred), not from reading omp's code.

**Merging duplicates:** OpenUsage merges logins found in several places by
identity key: one account, with each place as a further source
(`ProviderAccountAssembly+Codex.swift` 90-148;
`ProviderAccountAssembly.swift` 134-176).

## On this machine

- **Claude:**
  - Default home only: `~/.claude.json` has `oauthAccount`; there is no
    `.credentials.json`, the credential is in the Keychain.
  - Claude Desktop is present, with one account directory.
  - Claude Swap is absent.
- **Codex:** `~/.codex/auth.json` only. No other homes, no Keychain item,
  no codex-swap.
- **omp:** as above.
- **Environment of live processes:**
  - The `claude` process has no `CLAUDE_CONFIG_DIR`, so it uses the default
    home.
  - The ten omp processes have neither variable.
  - Of six `codex` processes (desktop and editor apps, none in tmux), two
    set `CODEX_HOME=~/.codex`.

## Can a pane be tied to an account?

| Harness | Answer | Chain | Weakness |
|---|---|---|---|
| **omp** | **Yes, exactly** (verified) | Pane ID, then `~/.omp/agent/terminal-sessions/tmux-%N` (line 1 cwd, line 2 session file), then the session's latest assistant `message.credentialId`, then `auth_credentials.id`, then its identity | none: it's recorded for every request, so a mid-session switch shows |
| **Claude Code** | **Yes, as an inference** | Pane PID, then `CLAUDE_CONFIG_DIR` or the default home, then that home's `.claude.json` `oauthAccount`, giving `accountUuid|orgUuid` | shows the *current* login. A re-login or org switch after the process started would misattribute it. Transcript owner fields help when present |
| **Codex** | **Yes, as an inference** | Pane PID, then `CODEX_HOME` or the default homes, then `auth.json` `tokens.account_id` or its claim | same as Claude. Rollout files record only `plan_type`, never the account |

omp also writes a `credential_pin` record (a 64-hex SHA-256 of the account
and org scope). It isn't reproducible from `identity_key` alone, and the
`credentialId` route makes it unnecessary.

## Consequences for the plan

- **C5 is viable:** exact for omp, inferred for Claude and Codex. The
  inference is recorded with confidence `inferred`, and it's upgraded when
  a transcript carries owner fields.
- **D7 identity:**
  - Claude accounts are `accountUuid|orgUuid`, which is org-aware and
    covers personal versus team.
  - Codex accounts are `account_id`.
  - omp is its `identity_key`.
  - One account seen by several harnesses must merge, which needs a common
    key. The provider's own account ID works; omp's email-based key needs
    mapping through its `accountId` and `orgId` fields.
  - All of them are hashed before storing (D5).
- **Identity needs no network call for Claude** (the state file), and none
  for Codex (`account_id`), so discovery doesn't spend the usage
  endpoint's allowance.
- See [R8](R8-omp-authentication.md) for the alternative of usage-watch
  holding its own logins.

## Open

1. Can Claude Code or Codex switch account mid-process without a
   restart? That decides whether "home at start = account" is safe.
2. Should a token set in the environment (`CLAUDE_CODE_OAUTH_TOKEN`,
   `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) override the home? Checking
   whether those names are *present* is safe without reading them.
3. Codex processes outside tmux (the desktop and editor apps) are out of
   scope for pane attribution, but their usage still counts toward the
   account.

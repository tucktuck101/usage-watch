# R8: How does omp authenticate, and should usage-watch hold its own logins?

Date: 2026-09-30. Evidence: omp 18.4.2's embedded source (a Bun bundle,
read with `strings`), its `--help` output, key names and counts from its
database, and the providers' published terms. omp is
[oh-my-pi](https://github.com/can1357/oh-my-pi), a fork of pi. No login
was started and no credential value was read.

## How omp does it

**Login** (verified):
- **Anthropic:** OAuth authorization code with PKCE. It authorizes at
  `claude.ai/oauth/authorize`, exchanges tokens at
  `api.anthropic.com/v1/oauth/token`, and uses a local callback on port
  54545 (or pasting the code). **It uses Claude Code's own OAuth client
  ID**, and Claude Code's User-Agent when refreshing.
- **OpenAI Codex:** PKCE at `auth.openai.com`, **with Codex CLI's client ID**,
  callback on port 1455 (the same fixed port as Codex CLI), and a
  device-code flow as an alternative.
- omp has no OAuth client of its own. It presents itself as each vendor's
  first-party CLI.

**Storage and refresh** (verified):
- **Storage:** `auth_credentials.data` holds access and refresh tokens,
  expiry, account, email and org. `identity_key` is `email:…|org:…`, so it
  is org-aware.
- **Refresh across processes:** a lease (15 s, renewed every 5 s)
  guarantees only one process refreshes a login. The new token is written
  with a compare-and-swap, so a process that loses the race picks up the
  winner's token. That is necessary because refresh tokens rotate.
- **Revocation:** a refresh error such as `invalid_grant` or `revoked` sets
  `disabled_cause`, and the login is excluded until you log in again.
- **Exhausted limits:** a usage report showing a limit exhausted writes a
  block until that window resets.

**Choosing a login** (verified):
- A session sticks to one login for 60 minutes.
- Otherwise it ranks logins by live usage: not blocked, then plan, then
  per-account reserve, then priority, then the primary window below 85%
  used, and so on.
- `omp dry-balance` simulates the choice.

**Commands** (help output):
- `omp login [provider]` adds a login.
- `omp auth-broker list|logout` lists and removes logins.
- **`omp usage --json [--redact] [--history]` reports usage for every
  account omp holds.**

## Terms of service: the deciding finding

Anthropic's
[Claude Code legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance),
section "Authentication and credential use", verified verbatim on
2026-09-30:

> OAuth authentication is intended exclusively for purchasers of Claude
> Free, Pro, Max, Team, and Enterprise subscription plans and is designed
> to support ordinary use of Claude Code and other native Anthropic
> applications.

> Anthropic does not permit third-party developers to offer Claude.ai
> login into their own applications, or to route requests through Free,
> Pro, or Max plan credentials on behalf of their users. Moreover,
> developers may not collect, store, or intermediate Claude.ai credentials
> or session tokens — sign-in to a Claude account must complete through
> Anthropic's own flow.

> Anthropic reserves the right to take measures to enforce these
> restrictions and may do so without prior notice.

What this means for usage-watch, a published tool:

- **Its own Claude logins (omp's method): not permitted.** It would mean
  offering Claude.ai login in a third-party app, with Claude Code's client
  ID.
- **Reading Claude Code's existing token to call the usage endpoint (C1 as
  planned): also not permitted.** Reading a session token from the Keychain
  or a file and sending it is collecting and intermediating a Claude.ai
  session token.
- The [0.1](0.1-endpoint-check.md) probe was a one-off check that the owner
  ran on their own machine, and it is not part of the tool. The tool must
  not do it.

**OpenAI:** no published policy was found that either permits or forbids
reusing Codex CLI's client or its tokens (inferred as tolerated for
personal use, unconfirmed).

## Comparison

| | Own logins | Read the harnesses' logins | Read omp's usage record | No credentials at all |
|---|---|---|---|---|
| Claude | **not permitted** | **not permitted** | permitted: usage-watch never touches a credential, and omp's own behaviour is omp's concern | permitted |
| Codex | unclear, and port 1455 clashes with `codex login` | unclear | permitted | permitted |
| Setup | a browser login per account | none | needs omp | none |
| Several accounts | natural | as many as the harnesses hold | as many as omp holds | limited |

## Consequences for the plan

- **Drop every credential-based Claude capacity path from C1.** Claude
  capacity must come from sources that involve no Claude credential:
  - omp's `usage_history` table, or `omp usage --json`;
  - the reset time on a stalled pane's screen;
  - anything Claude Code itself exposes locally (open: R9).
- **Codex:** capacity rides free on its own session files
  (`payload.rate_limits` on `token_count` events, [R2](R2-local-token-records.md)),
  with no credential needed. Prefer that. Whether to use Codex's login for
  live reads is a separate decision, given the unclear policy.
- **Do not build usage-watch's own logins** for either provider.
- **Token usage (C2) is unaffected:** it reads the harnesses' session
  files, which are not credentials.

## Open

1. **R9 (new):** does Claude Code expose its own plan limits locally,
   without a credential? For example, its status line input, a local cache,
   or an OTel signal.
2. Is `omp usage --json` a stable interface?
3. OpenAI's position on third-party use of Codex CLI tokens.

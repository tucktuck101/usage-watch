# D5: Privacy

Status: draft, revised after review (2026-10-01). Scope markers as in
[D1](D1-model.md).

## Three classes of data [F]

| Class | Examples | Local store | Export |
|---|---|---|---|
| **Forbidden** | credentials of any kind; prompts, responses, system instructions, tool inputs and outputs, file contents; raw OTLP bodies; raw emails and account, org or user IDs | **never** | **never** |
| **Developer context** | repository name and remote, branch, worktree, task, issue, PR, project name, `cwd` and other paths | yes | **opt-in per field**, conservative defaults (below) |
| **Measurements** | token counts, capacity percentages, cost amounts, model, provider, harness, role, timestamps, hashed identities | yes | yes, within D4's cardinality rules |

## Ingestion is an allowlist [F]

- **Collectors and the OTLP receiver keep only fields they have a mapping
  for** (D4's inbound table). Everything else is dropped on arrival. A new
  harness field, including a new content field, is dropped by default.
- **Raw bodies are never persisted.** OTLP payloads and session-file lines
  are parsed in memory. Only mapped fields reach the store.
- **Errors never include payloads.** A parse failure is logged as the
  source, the field path and a size, never values.
- The list of known content fields (`gen_ai.input.messages`,
  `gen_ai.output.messages`, `gen_ai.system_instructions`,
  `gen_ai.response.text`, `user_prompt.prompt`,
  `assistant_response.response`, `codex.user_prompt.prompt`) is kept as a
  **test**: none of them may ever map to a stored field.
- Collectors can't reach credentials. For omp, only named columns are
  selected, never `SELECT *`.

## Hashed identities [F]

- **The hash:** `HMAC-SHA256(install_secret, provider + ":" + raw_id)`,
  truncated to 16 hex characters. Used for account aliases (D7),
  `checkout_id` and `repository_id` (D2).
- **`install_secret`:** 32 random bytes, created on first run, file mode
  0600, in the state directory.
- **Why it's keyed:** it can't be reversed by guessing, and it can't be
  linked across installs.
- **What that costs:** losing the secret splits history, so the state
  directory is what to back up.
- **Display:** the user's label, else `<provider> account <first 6 of key>`.

## Export defaults [L, defaults fixed now]

- Off by default.
- When on, it sends measurements, plus only the developer-context fields
  the user switches on:

| Field | Default | When switched on |
|---|---|---|
| project | off | display name only |
| repository | off | hashed `repository_id` only; the remote URL is never exported |
| branch | off | as-is, or sanitised to its prefix (e.g. `fix/…`), user's choice |
| task, issue, PR | off | as-is; free text, so opt-in only |
| paths, `cwd` | never | none |
| account | hashed key | label only if the user allows it |

- Export has its own allowlist of D1 fields, so a field added to the store
  later doesn't leave the machine until it's added to that list.

## Diagnostics [F]

`doctor --capture` keeps its redaction: home directory, request IDs,
emails. Logs never contain forbidden-class data. Messages name files and
fields, never values.

## Deletion [L]

`usage-watch forget --account|--project|--before` deletes matching records.
The keys in D3 are chosen to make it simple.

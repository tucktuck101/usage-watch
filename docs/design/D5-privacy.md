# D5: Privacy

Status: draft. Rests on the plan's principle of no credentials of any kind,
and on [R1's live capture](../research/R1-native-otel.md#live-capture-2026-10-01)
(which identity attributes arrive).

## Never stored, never logged

- **Credentials of any kind:** tokens, keys, credential files, Keychain
  items, the secret columns of other tools' databases. Collectors are
  written so they can't reach them. For omp, only the named columns are
  selected, never `SELECT *`.
- **Content:** prompts, responses, system instructions, tool inputs and
  outputs, file contents. The receiver **drops** any OTel attribute known
  to carry content (`gen_ai.input.messages`, `gen_ai.output.messages`,
  `gen_ai.system_instructions`, `gen_ai.response.text`,
  `user_prompt.prompt`, `assistant_response.response`, `codex.user_prompt.prompt`),
  even when a harness has content capture switched on.
- **Raw identities:** emails, account IDs, org IDs and user IDs are
  replaced by their hashed key the moment they are read. The raw value is
  never written, logged or shown.

## Hashing identities

- **`HMAC-SHA256(install_secret, provider + ":" + raw_id)`, truncated to 16
  hex characters.**
- **`install_secret`:** 32 random bytes created on first run, in the state
  directory with file mode 0600.
- **Why a keyed hash:** a plain hash of an email or UUID can be reversed by
  guessing. It would also link the same account across different people's
  exports.
- **What a keyed hash gives up:** it can't be reversed, and it's stable on
  this machine. A reinstall that loses the secret gives new keys, which
  also splits history. The state directory is what to back up.
- **Display:** the user's own label (D7), else `<provider> account
  <first 6 of key>`.

## What's allowed

Local, non-secret facts the views need: `cwd`, project and repository
names, branch, model, token counts, costs, timestamps, window percentages.
All of these stay in the local database.

## Export (E1)

- Off by default. When switched on, nothing leaves except aggregates and
  events built from D1 records.
- **Paths are reduced to project names.** `cwd` is never exported.
- Identity is the hashed key, or the label if the user allows it.
- The D4 cardinality caps apply.

## Diagnostics

`doctor --capture` keeps its redaction: home directory, request IDs and
emails. Log lines never include raw identities. Error messages name files
and fields, never values.

## Deletion

`usage-watch forget --account <label>|--project <name>|--before <date>`
deletes matching records. It's a later task, noted here so the schema keys
make it simple.

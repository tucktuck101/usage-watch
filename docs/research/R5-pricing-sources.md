# R5: Where does pricing data come from, for estimating cost?

Date: 2026-09-30. Sources are linked inline.

## Sources

| Source | Machine-readable | Licence | Coverage | Notes |
|---|---|---|---|---|
| [LiteLLM model price table](https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json) | Yes, JSON, USD per token | [MIT](https://raw.githubusercontent.com/BerriAI/litellm/main/LICENSE) (except `enterprise/`) | 4,437 entries, including current Claude (opus-5-5, sonnet-5-5, fable-5-1, haiku-4-5) and OpenAI and Codex models (to gpt-5.3-codex) | [Changes several times a day](https://api.github.com/repos/BerriAI/litellm/commits?path=model_prices_and_context_window.json). OpenUsage and ccusage both use it |
| [OpenRouter models API](https://openrouter.ai/api/v1/models) | Yes, JSON; prices as strings | Terms of the API | 464 models seen | Documented as needing a key, but answered without one. Fields: `prompt`, `completion`, `input_cache_read`, `input_cache_write`, `input_cache_write_1h` |
| [Anthropic pricing page](https://platform.claude.com/docs/en/about-claude/pricing) | No | n/a | Official | Cache writes 1.25x (5 min) or 2x (1 h) input; cache hits 0.1x (0.05x Opus 5.5, 0.025x Fable 5.1); batch 50% off; US-only inference 1.1x |
| [OpenAI pricing page](https://developers.openai.com/api/docs/pricing) | No | n/a | Official | Input, cached input, output; reasoning billed as output |
| The harnesses' own cost figures | Via OTel ([R1](R1-native-otel.md)) | n/a | Claude Code `cost_usd`, Codex `codex.turn.cost_microusd`, omp `…estimated_usd` | Each harness's own estimate, at its own price table |

**LiteLLM's entry fields:**
- `input_cost_per_token`, `output_cost_per_token`,
  `cache_read_input_token_cost`, `cache_creation_input_token_cost`,
  `cache_creation_input_token_cost_above_1hr`, plus tier variants
  (`_batches`, `_priority`, `_flex`, `_above_200k_tokens`).
- Only about 70 entries price reasoning separately
  (`output_cost_per_reasoning_token`), none of them Claude or OpenAI.
- Spot checks matched the official pages. Example: `claude-opus-4-5` is
  input 5e-06, output 2.5e-05, cache read 5e-07, cache write 6.25e-06.

**ccusage** prices from LiteLLM plus models.dev, bundles a copy for
offline use, and has three modes ([cost modes](https://ccusage.com/guide/cost-modes)):
`auto` (the harness's own cost where present), `calculate` and `display`.

## Subscription usage is not billed per token

Claude Code's [cost docs](https://code.claude.com/docs/en/costs) say that
for Pro and Max, usage is included in the subscription and the session cost
figure "isn't relevant for billing purposes". The dollar figure is computed
locally at list price and "is an estimate". Team and Enterprise usage
within a seat's allowance "isn't metered in dollars". The equivalent
statement for ChatGPT plans was not checked.

## Consequences for the plan

- **K1:** use LiteLLM's table as the pricing source. It is MIT, current, and
  machine-readable. Bundle a pinned snapshot so the tool works offline, and
  add a manual `refresh` command. Don't fetch it on every run: it changes
  several times a day, and results must be reproducible.
- **K2:** every cost record is labelled:
  - `actual`: billed API spend, from a provider's spend fields (see the
    Claude `spend` object in [0.1](0.1-endpoint-check.md));
  - `estimated`: tokens times list price;
  - `harness_estimate`: the harness's own figure.

  They are never added together, and subscription usage is always
  `estimated`. The value to a subscriber is "what this would have cost at
  API prices", which compares projects and sessions. It is not a bill.
- **History is priced at the rate that applied then.** Each estimate records
  which table version and entry it used, so a later price change doesn't
  silently rewrite old numbers.
- **Cached tokens have their own prices**, so estimates need the cache
  split from R2, not only totals.

## Open

1. Should models.dev be a second source, or a cross-check?
2. How to price a model the table doesn't list yet: mark the cost
   unknown, never zero.

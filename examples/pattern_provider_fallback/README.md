# pattern_provider_fallback

**Tier:** Pattern | **Credential requirement:** `ANTHROPIC_API_KEY` (and `OPENAI_API_KEY` or a local Ollama instance for the fallbacks); none with `--offline`

Demonstrates provider-neutral multi-provider fallback with visible routing decisions. The `resilient` agent attempts Anthropic first, then OpenAI, then a local Ollama model.

Each profile declares an operational context limit, with smaller windows on the fallbacks. Before
dispatch, Runtime reserves 4,096 output tokens plus tool/schema overhead and a safety margin, then
packs each candidate independently. Required context that cannot fit causes that candidate to be
skipped. The example limits are deployment choices; adjust them to your serving configuration.
The deterministic mode also exercises explicit windows with a 256-token output reservation.

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.12+ | — |
| `ANTHROPIC_API_KEY` | The primary's key is resolved before the call; a missing key is a configuration error, which does not fall back |

Set the credentials you have:

```bash
export ANTHROPIC_API_KEY=...   # Primary
export OPENAI_API_KEY=...      # Cloud fallback
# Ollama on http://127.0.0.1:11434 is the local fallback (no key needed)
```

Without `ANTHROPIC_API_KEY` the CLI stops with a hint to use `--offline`.

## Setup

```bash
pip install -e '.[dev]'
```

## Run

```bash
python -m examples.pattern_provider_fallback.app "What year did the first moon landing occur?"
```

**Expected output** (example with Anthropic as primary). The route is the provider and model of the
run's last `model.completed` event:

```
[Route: anthropic/claude-sonnet-4-5]
The first moon landing occurred in 1969, when Apollo 11 landed on the lunar surface on July 20th.
```

## Offline: a planned failure

`--offline` (also accepted as `--deterministic`) needs no credentials: a planned primary failure
followed by the in-memory mock fallback:

```bash
python -m examples.pattern_provider_fallback.app --offline
```

This uses the real `ModelRouter` and prints `mock/deterministic` as the selected route. The default
mode continues to demonstrate the configured primary (Anthropic) → fallback (OpenAI) → local (Ollama) chain.

## Test

```bash
python -m pytest tests/unit/test_example_workflows.py tests/unit/test_context_budget.py -q
```

## Reset

No persistent state. Nothing to reset.

## Production gaps

- Cost and latency budgets are not declared per-provider.
- Provider selection criteria (cost tier, quality tier, latency) are not exposed in this minimal configuration.

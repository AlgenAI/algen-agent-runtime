# pattern_provider_fallback

**Tier:** Pattern | **Credential requirement:** At least one of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or a local Ollama instance

Demonstrates provider-neutral multi-provider fallback with visible routing decisions. The `resilient` agent attempts Anthropic first, then OpenAI, then a local Ollama model.

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | — |
| At least one provider credential | See secrets below |

Set the credentials you have:

```bash
export ANTHROPIC_API_KEY=...   # Primary
export OPENAI_API_KEY=...      # Cloud fallback
# Ollama on http://127.0.0.1:11434 is the local fallback (no key needed)
```

## Setup

```bash
pip install -e '.[dev]'
```

## Run

```bash
python -m examples.pattern_provider_fallback.app "What year did the first moon landing occur?"
```

**Expected output** (example with Anthropic as primary):

```
[Route: anthropic/claude-sonnet-4-5]
The first moon landing occurred in 1969, when Apollo 11 landed on the lunar surface on July 20th.
```

## TODO — Deterministic failure scenario

The current configuration does not simulate a primary-provider failure. To demonstrate routing in CI:

1. Configure the primary provider with an invalid endpoint or inject a mock error.
2. Assert that the `model_used` field in the result matches the expected fallback.
3. Show the routing decision in telemetry spans.

## Test

```bash
python -m pytest examples/pattern_provider_fallback/tests/ -q
```

## Reset

No persistent state. Nothing to reset.

## Production gaps

- No deterministic mock mode: running without paid credentials may fall through to Ollama silently.
- Cost and latency budgets are not declared per-provider.
- Provider selection criteria (cost tier, quality tier, latency) are not exposed in this minimal configuration.

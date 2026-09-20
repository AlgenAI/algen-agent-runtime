# pattern_provider_fallback

**Tier:** Pattern | **Credential requirement:** At least one of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or a local Ollama instance

Demonstrates provider-neutral multi-provider fallback with visible routing decisions. The `resilient` agent attempts Anthropic first, then OpenAI, then a local Ollama model.

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.12+ | — |
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

## Deterministic failure scenario

The CLI includes a credential-free planned primary failure followed by an in-memory fallback:

```bash
python -m examples.pattern_provider_fallback.app --deterministic
```

This uses the real `ModelRouter` and prints `mock/deterministic` as the selected route. The default
mode continues to demonstrate the configured Anthropic → OpenAI → Ollama chain.

## Test

```bash
python -m pytest examples/pattern_provider_fallback/tests/ -q
```

## Reset

No persistent state. Nothing to reset.

## Production gaps

- Cost and latency budgets are not declared per-provider.
- Provider selection criteria (cost tier, quality tier, latency) are not exposed in this minimal configuration.

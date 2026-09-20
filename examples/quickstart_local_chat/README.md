# quickstart_local_chat

**Tier:** Quickstart | **Credential requirement:** None (Ollama only)

Run a conversational agent entirely on your machine using [Ollama](https://ollama.com). No cloud API key is needed.

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | — |
| [Ollama](https://ollama.com/download) running on `http://127.0.0.1:11434` | Default install |
| `llama3.2` model pulled | `ollama pull llama3.2` |

For the `ollama-private` mode also pull `qwen3:8b`:

```bash
ollama pull qwen3:8b
```

## Setup

```bash
pip install -e '.[dev]'
```

## Run

```bash
# Default mode — llama3.2
python -m examples.quickstart_local_chat.app "What is the capital of France?"

# Privacy-first mode — qwen3:8b with strict model allowlist
python -m examples.quickstart_local_chat.app --mode ollama-private "Explain what a transformer model is."
```

**Expected output (default mode):**

```
The capital of France is Paris.
```

## Test

```bash
python -m pytest examples/quickstart_local_chat/tests/ -q
```

## Reset

This example holds no persistent state. Nothing to reset.

## Architecture

```
app.py  →  AlgenAgentRuntimeClient  →  OllamaProvider  →  local Ollama server
```

| Mode | Agent | Model |
|---|---|---|
| `ollama` (default) | `minimal` | `llama3.2` |
| `ollama-private` | `local-private` | `qwen3:8b` |

`ollama-private` uses a `model_allowlist` to prevent the runtime from routing requests to any cloud provider.

## Production gaps

- No authentication or multi-tenant isolation — suitable for local development only.
- No retry policy on Ollama availability failures.
- Memory policy is disabled; conversation history is not persisted across runs.

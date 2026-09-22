# Provider extension guide

Implement the `ModelProvider` protocol without returning SDK objects:

- `provider_id`
- `capabilities(model)`
- `generate(ModelRequest) -> ModelResponse`
- `stream(ModelRequest) -> AsyncIterator[ModelStreamEvent]`
- `embed(texts, model)`
- `health()`

Translate provider errors to `ProviderError` with an `ErrorKind` and a correct `retryable` flag. Put provider-only inputs in `ModelRequest.extensions`; never add them to orchestration contracts. Raw metadata is returned only when `raw_response_enabled` is true. Register the adapter with `ModelRouter.register_provider`; orchestration requires no edit.

OpenAI-compatible providers can reuse `OpenAICompatibleProvider`. Native protocols should implement the interface directly or override payload/response/stream conversion. Capability declarations must be conservative and should be covered by contract tests.

OpenAI and Azure OpenAI strict structured outputs are normalized at the provider boundary. Runtime
requires every object property on the wire, forbids additional properties, removes defaults and
constraints unsupported by the provider subset, and converts `const` to a single-value enum. The
original application schema is retained for Runtime's full post-response validation; provider wire
compatibility never weakens the application contract.

`MistralProvider` is an example of a dedicated compatible adapter: it reuses normalized chat, tool, streaming, structured-output, and embedding handling while translating Mistral-specific multimodal image blocks. Configure it with `type: mistral` and an `env://MISTRAL_API_KEY` secret reference. Capability declarations can be narrowed in YAML for the selected Mistral model.

Secrets are references (`env://NAME`) resolved immediately before a request. Never accept or log literal secret configuration.

## Provider instances vs. adapter types

Configuration keys under `providers:` in `agent.yaml` represent **provider instance IDs** (deployment identities), while the `type:` field specifies the underlying adapter implementation.

Multiple instances of the same adapter type can be configured and routed independently:

```yaml
providers:
  local-fast:
    type: ollama
    base_url: http://127.0.0.1:11434/v1
    default_model: llama3.2:1b
    cost_per_1k_input: 0.0
    cost_per_1k_output: 0.0
  local-heavy:
    type: ollama
    base_url: http://127.0.0.1:11434/v1
    default_model: llama3.3:70b
    cost_per_1k_input: 0.0
    cost_per_1k_output: 0.0
```

`ModelRouter.register_provider()` accepts an optional `registration_id`. When omitted (e.g. direct programmatic usage), it defaults to `provider.provider_id` for backward compatibility. Registering the same `registration_id` twice raises `ValueError`.

Each registration maintains independent health tracking, circuit breaking, sliding-window rate limits, token cost accounting, and capability cache partitions.

## Capability narrowing

Provider capabilities can be restricted in configuration via the `capabilities:` block. Runtime enforces consistent capability narrowing:

$$C_{effective}(m) = C_{adapter}(m) \cap C_{configured}$$

Configuration can turn `true` into `false` (e.g., disabling streaming or tools on a specific endpoint). It **cannot** turn an adapter-reported `false` into `true`. Attempts to widen an unsupported capability are logged and kept `false`.

```yaml
providers:
  internal-openai:
    type: openai_compatible
    base_url: http://vllm.internal:8000/v1
    default_model: mistral-7b-instruct
    capabilities:
      streaming: false  # narrow streaming off even if endpoint supports it
      tools: false      # disable tool calling for this deployment
```

> [!WARNING]
> An omitted `capabilities` block preserves adapter discovery and model-specific defaults. Only specify `capabilities` when intentionally constraining features for governance, compliance, or backend compatibility.

## Provider reference validation

At settings load time, `AppSettings` validates that all provider references resolve to configured provider instance keys:

- `agents[*].default_model.provider`
- `agents[*].fallback_models[*].provider`
- `retrieval[*].embedding_provider` (when non-empty)

If any reference points to an unregistered provider, `load_settings()` fails immediately with a `ConfigurationError` identifying the exact configuration path and candidate providers. Typographical errors never survive configuration loading to become runtime routing failures.


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

## Hosted OpenAI-compatible vendors

`gemini`, `xai` and `groq` are first-class types: the vendor base URL and default secret are built in,
and `base_url`, `capabilities` and `stream_usage` can still be set.

```yaml
providers:
  gemini: {type: gemini, default_model: gemini-flash-latest}        # env://GEMINI_API_KEY
  groq: {type: groq, default_model: openai/gpt-oss-120b}            # env://GROQ_API_KEY
  xai: {type: xai, default_model: grok-4}                           # env://XAI_API_KEY
```

## When the router falls back

A model call that fails moves on to the next candidate (by rank, or by declaration order with
`ordered`) when the failure is specific to that provider: authentication or authorization errors,
rate limits, timeouts (including HTTP 408), 5xx and transport failures, an invalid response, or an
HTTP error such as 404 for a model the provider does not serve. A request the provider rejects as
invalid (HTTP 400 or 422, `invalid_request`) is raised without trying other models, since they
would reject it too, and it does not count against the provider's circuit breaker. A streamed call
falls back only before it starts, and only when the request does not fit a candidate's context
budget.

## Ordered fallback

By default (`routing_preference: balanced`) candidates are ranked by `quality_tier`, observed
latency and cost, so a consistently faster fallback can be chosen ahead of the default model. Set
`routing_preference: ordered` on the default model and every fallback to try them strictly in
declaration order; providers with an open circuit breaker, missing capabilities or exceeded
latency/cost limits are still skipped. `ordered` describes the whole list, so configuration that sets
it on only some of an agent's profiles is rejected at load time. When profiles are built in code, the
first (default) profile decides the mode.

```yaml
default_model: {name: primary, provider: gemini, model: gemini-flash-latest, routing_preference: ordered}
fallback_models:
  - {name: fast, provider: groq, model: openai/gpt-oss-120b, routing_preference: ordered}
```

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

Partial capability narrowing is supported: specifying a subset of capability fields narrows only those explicitly declared fields. Omitted fields inherit adapter discovery:

```yaml
providers:
  internal-openai:
    type: openai_compatible
    base_url: http://vllm.internal:8000/v1
    default_model: mistral-7b-instruct
    capabilities:
      tools: false  # disables only tools; chat, streaming, and other supported capabilities remain enabled
```

> [!WARNING]
> An omitted `capabilities` block preserves adapter discovery and model-specific defaults. Only specify `capabilities` when intentionally constraining features for governance, compliance, or backend compatibility.

## Provider reference validation

At settings load time, `AppSettings` validates that all provider references resolve to configured provider instance keys:

- `agents[*].default_model.provider`
- `agents[*].fallback_models[*].provider`
- `retrieval[*].embedding_provider` (when non-empty)

If any reference points to an unregistered provider, `load_settings()` fails immediately with a `ConfigurationError` identifying the exact configuration path and candidate providers. Typographical errors never survive configuration loading to become runtime routing failures.

## Model context budgets

Each model profile may declare `context_window_tokens` and `context_safety_margin_tokens` (default
256). Set the window to an operational limit supported by your deployed model. Runtime does not
infer capacities from model names. For example, within an agent definition:

```yaml
default_model:
  name: primary
  provider: cloud
  model: deployed-model
  context_window_tokens: 32000
  context_safety_margin_tokens: 512
fallback_models:
  - name: local
    provider: local
    model: deployed-local-model
    context_window_tokens: 12000
    context_safety_margin_tokens: 512
budget:
  max_output_tokens: 4096
```

These example limits are deployment choices, not advertised model capacities. Configure every
fallback that should have a limit. Omitting `context_window_tokens` preserves existing behavior;
it does not imply that a model has an unlimited window.

Immediately before dispatch the router computes:

```text
message allowance = context window - requested output tokens - safety margin
                    - estimated tool definitions, response schema, and extensions
```

Packing protects system messages, the latest user message, and the newest message group. Tool calls
and results stay together. Older groups can be dropped; the output reservation is never silently
reduced. Each candidate is packed independently from the original request, so a small primary cannot
remove history from a larger fallback. Run state is unchanged by router packing.

Candidates whose required context cannot fit are skipped before rate limiting or provider calls,
without affecting provider health. If all eligible candidates exceed their budgets, the router raises
`BudgetExceededError`. If a fitting provider was called and failed, provider error handling remains
in effect. Streaming can select a fitting fallback during preflight, but does not switch providers
after streaming dispatch. Output-limit recovery retries and response repairs pass through the same
budget check.

The built-in estimator uses normalized serialized content sizes, not a provider tokenizer. The safety
margin accommodates some wire overhead but cannot guarantee exact model token counts. Builder limits
still apply earlier; increasing a profile's window cannot recover messages already evicted there.
Configured budgets currently support text messages only: image/file blocks raise `ConfigurationError`
because URL length cannot represent their token cost. Direct router callers must supply a positive
`ModelRequest.max_output_tokens`; Runtime supplies it from the agent budget or request override.

When budgeting is enabled, profile extensions cannot override normalized input, model, tools,
response format, or output-limit fields. Such overrides raise `ConfigurationError` rather than
bypassing the estimate. Other extensions are included in the estimated overhead. No optional tokenizer
or provider SDK dependency is required.


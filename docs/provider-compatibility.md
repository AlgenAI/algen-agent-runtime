# Model provider compatibility

Capabilities depend on the selected model and server. These adapter defaults are conservative; deployments may narrow generic endpoint declarations.

| Adapter | Text/chat | Stream | Tools | JSON schema | Embeddings | Images | Endpoint style |
|---|---:|---:|---:|---:|---:|---:|---|
| OpenAI | yes | yes | yes | yes | yes | yes | OpenAI |
| Azure OpenAI | yes | yes | yes | yes | yes | model-dependent | Azure OpenAI |
| Anthropic | yes | yes | yes | yes | no | yes | Messages |
| DeepSeek | yes | yes | model-dependent | no strict guarantee | endpoint-dependent | no | OpenAI-compatible |
| Mistral AI | yes | yes | model-dependent | model-dependent | yes | model-dependent | Mistral Chat/Embeddings |
| Google Gemini | yes | yes | yes | yes | yes | yes | OpenAI-compatible (Gemini API) |
| xAI Grok | yes | yes | yes | yes | no | yes (model-dependent) | OpenAI-compatible |
| Groq | yes | yes | yes | gpt-oss models (others best-effort) | no | no | OpenAI-compatible |
| Ollama | yes | yes | model-dependent | model-dependent | yes | model-dependent | OpenAI-compatible |
| Hugging Face Inference | yes | yes | model-dependent | no guarantee | endpoint-dependent | model-dependent | OpenAI-compatible router |
| Local Transformers | yes | completion only | no | no | no | no | in-process |
| Generic OpenAI-compatible | configured | configured | configured | configured | configured | configured | OpenAI-compatible |
| Mock | yes | yes | yes | yes | yes | yes | deterministic in-process |

An unsupported requested feature fails with `CapabilityError` before provider invocation.

## Streaming usage

OpenAI-style adapters ask for token usage on streamed calls (`stream_options.include_usage`), so
budgets and metering count them. A server that rejects the field is configured with
`stream_usage: false` on its provider entry; Mistral defaults to off because its API reports usage
unasked. `stream_options` from profile extensions are kept.

## Gemini notes

- `type: gemini` calls `https://generativelanguage.googleapis.com/v1beta/openai`; set `base_url` to
  use another endpoint. Thinking models accept `reasoning_effort` (`low`, `medium`, `high`) as a
  profile extension; without it, a small `max_output_tokens` can be spent on hidden reasoning.
- The endpoint keeps only the last system message, so the adapter merges the leading run of system
  messages (agent instructions, retrieved evidence, pinned compaction summaries), in order, into
  one. A system message later in the conversation is sent in place as a user message prefixed
  `[system] ` so it keeps its position.
- Embedding models (`gemini-embedding-*`) report `chat: false, embeddings: true`.

## Groq notes

- Groq enforces strict JSON-schema output only on the gpt-oss family (`openai/gpt-oss-*`). For
  those models the adapter reports `json_schema: true` and sends `strict: true`; for other models it
  reports `json_schema: false` (best-effort `structured_output` only) and sends `strict: false`, so
  a `response_schema` is not rejected and Runtime's JSON-schema verifier checks the answer.
- Setting `capabilities` on the provider entry replaces this per-model default; `json_schema` in it
  then decides `strict`. Narrow `tools` there for Groq models without tool calling.

## xAI notes

- The adapter reports `images: true`. Not every Grok model accepts image input; narrow
  `capabilities` on the provider entry for a text-only model.

## Anthropic notes

- `response_schema` is sent as `output_config.format` (`json_schema`). Keywords the endpoint does not
  accept (numeric and length bounds, `pattern`, `default`, unknown string formats) are removed and
  every object is closed with `additionalProperties: false`. Each removed constraint is described
  in that property's `description` instead (`At most 3 items.`, `Between 0 and 4.`), so the model
  still sees it; the Runtime's `json_schema` verifier still checks the answer against the full
  original schema. OpenAI's strict schemas get the same descriptions.
- A request never ends with an assistant turn: Claude 5.x models reject that as a prefill (HTTP
  400), so the adapter adds a `Continue.` user turn if one would.
- Profile `extensions` are merged into the request body; an `output_config` extension such as
  `{effort: low}` is merged with the response format rather than replacing it.
- Results of parallel tool calls are sent together in one user turn.
- `stop_reason` maps to `finish_reason`: `max_tokens` → `length`, `refusal` → `content_filter`.
- Thinking blocks are never sent back. Current Claude models bind each block to the conversation
  that produced it, and Runtime guardrails may rewrite history between turns; omitting the blocks is
  always accepted, at the cost of the model reasoning afresh on each turn.
- Current Claude models reject non-default `temperature`; leave it unset for them.
- Prompt caching is on by default. The end of the system prompt carries a cache breakpoint, and the
  request asks for automatic caching of the growing conversation, so an agent loop's later calls
  read the tools, instructions and earlier turns from the cache (about a tenth of the input price).
  A gateway that rejects `cache_control` is configured with `prompt_caching: false` on its provider
  entry.
- Usage reports the whole prompt as `input_tokens` (Anthropic's uncached `input_tokens` plus
  `cache_creation_input_tokens` and `cache_read_input_tokens`) and the cache reads as
  `cached_tokens`, the same meaning as the OpenAI adapters, so budgets and metering count every
  input token.

## Output repair

When an answer fails verification and `verification_policy.max_repairs` allows another try, the
Runtime asks again with a **user** message named `runtime_repair`. It lists what failed as paths
and constraints only, never the answer's values (`- criteria[0].evidence: 4 items, at most 3`),
followed by each failed verifier's `repair_hint`. It is a user turn because providers that refuse
assistant prefill reject a conversation that ends with the model's answer. Session memory does
not keep it; hosts that show `state.messages` or context history can filter it out by its name.
A custom verifier puts value-free details in `VerificationResult.findings` to have them listed.

## Malformed tool calls

A model sometimes streams tool-call arguments that are not valid JSON, or not a JSON object, or
reaches its output-token limit in the middle of a call. Every adapter turns that into an
`InvalidToolCallError`, a retryable `ProviderError` of kind `invalid_response`. The error names the
tool and the parse error, never the arguments. The run retries the model call within the agent's
`retry_policy`. A call that was cut off by the output-token limit is retried with a higher limit,
like any other response that hit the limit. Each one is recorded as a `model.tool_call_invalid`
audit event (outcome `malformed` or `cut_off`) and counted by the
`algen_agent_runtime.model.tool_calls_invalid` metric (attributes `provider`, `model`,
`cut_off`). A malformed call does not count against the provider's circuit breaker.

## Capability narrowing semantics

Deployments configure provider instances under arbitrary mapping keys in `agent.yaml`. An optional `capabilities:` declaration in configuration applies uniform capability narrowing across all adapter types:

$$C_{effective}(m) = C_{adapter}(m) \cap C_{configured}$$

- **Narrowing:** If an adapter or model reports support (`true`), configuration can restrict it (`false`).
- **No widening:** If an adapter or model reports unsupported (`false`), configuration `true` is ignored and remains `false`, emitting a diagnostic warning.
- **Discovery preservation:** Omitting the `capabilities` block, or omitting individual fields in a partial `capabilities` block, preserves adapter discovery: omitted fields inherit adapter-reported capabilities rather than being set to false.


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
| xAI Grok | yes | yes | yes | yes | no | model-dependent | OpenAI-compatible |
| Groq | yes | yes | model-dependent | model-dependent | no | no | OpenAI-compatible |
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
- The endpoint keeps only the last system message, so the adapter merges every system message
  (agent instructions, retrieved evidence, compaction summaries), in order, into one.
- Embedding models (`gemini-embedding-*`) report `chat: false, embeddings: true`.

## Anthropic notes

- `response_schema` is sent as `output_config.format` (`json_schema`). Keywords the endpoint does not
  accept (numeric and length bounds, `pattern`, `default`, unknown string formats) are removed and
  every object is closed with `additionalProperties: false`; the Runtime's `json_schema` verifier
  still checks the answer against the full original schema.
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

## Capability narrowing semantics

Deployments configure provider instances under arbitrary mapping keys in `agent.yaml`. An optional `capabilities:` declaration in configuration applies uniform capability narrowing across all adapter types:

$$C_{effective}(m) = C_{adapter}(m) \cap C_{configured}$$

- **Narrowing:** If an adapter or model reports support (`true`), configuration can restrict it (`false`).
- **No widening:** If an adapter or model reports unsupported (`false`), configuration `true` is ignored and remains `false`, emitting a diagnostic warning.
- **Discovery preservation:** Omitting the `capabilities` block, or omitting individual fields in a partial `capabilities` block, preserves adapter discovery: omitted fields inherit adapter-reported capabilities rather than being set to false.


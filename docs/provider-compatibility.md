# Model provider compatibility

Capabilities depend on the selected model and server. These adapter defaults are conservative; deployments may narrow generic endpoint declarations.

| Adapter | Text/chat | Stream | Tools | JSON schema | Embeddings | Images | Endpoint style |
|---|---:|---:|---:|---:|---:|---:|---|
| OpenAI | yes | yes | yes | yes | yes | yes | OpenAI |
| Azure OpenAI | yes | yes | yes | yes | yes | model-dependent | Azure OpenAI |
| Anthropic | yes | yes | yes | yes | no | yes | Messages |
| DeepSeek | yes | yes | model-dependent | JSON mode, schema verified (see notes) | endpoint-dependent | no | OpenAI-compatible |
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

The table leaves out the `files` capability (PDF input a model reads): only the OpenAI, Anthropic
and Mock adapters report it; every other adapter refuses files unless its `capabilities` are
configured otherwise. See [attachments](attachments.md#6-providers) for how runs with files pick a model.

## Streaming usage

OpenAI-style adapters ask for token usage on streamed calls (`stream_options.include_usage`), so
budgets and metering count them. A server that rejects the field is configured with
`stream_usage: false` on its provider entry; Mistral defaults to off because its API reports usage
unasked. `stream_options` from profile extensions are kept.

## Azure OpenAI notes

- Every model is a deployment. `default_model` is the chat deployment; `embed(texts, model)`
  posts to `<base_url>/openai/deployments/<model>/embeddings?api-version=<api_version>`, with the
  key in the `api-key` header. Since 0.2.0b5 `embedding_deployment` on the provider entry names
  the deployment `embed()` uses when no model is given (retrieval or learned memory without an
  `embedding_model`); without either, `embed()` fails with a non-retryable `invalid_request`
  before any request. Before 0.2.0b5, `embed()` posted to `<base_url>/embeddings`, which Azure
  does not serve.

```yaml
providers:
  azure:
    type: azure_openai
    base_url: https://my-resource.openai.azure.com
    default_model: gpt-4o-mini              # the chat deployment
    embedding_deployment: text-embedding-3-small
```

## DeepSeek notes

- DeepSeek's `response_format` takes `text` or `json_object` only, not `json_schema`
  ([DeepSeek API: create chat completion](https://api-docs.deepseek.com/api/create-chat-completion),
  [JSON output](https://api-docs.deepseek.com/guides/json_mode)). Since 0.2.0b5 a
  `response_schema` is sent as `{"type": "json_object"}` with the schema in a system message after
  the agent's own (which also gives JSON mode the word "json" it requires), and the Runtime's
  `json_schema` verifier checks the answer against the full schema, as for any provider without
  `json_schema`. The adapter reports `structured_output: true, json_schema: false`. Before
  0.2.0b5 it sent a strict `json_schema` response format, which DeepSeek rejects.
- In JSON mode DeepSeek may return blank content; the Runtime treats an empty answer to a
  `response_schema` request as a failed response and retries it.

## Gemini notes

- `type: gemini` calls `https://generativelanguage.googleapis.com/v1beta/openai`; set `base_url` to
  use another endpoint, together with an explicit `api_key` (since 0.2.0b4 the default
  `env://GEMINI_API_KEY` is sent only to Google's endpoint; the same holds for `xai`, `groq` and
  `mistral`, see [providers](providers.md#hosted-openai-compatible-vendors)). Thinking models accept `reasoning_effort` (`low`, `medium`, `high`) as a
  profile extension; without it, a small `max_output_tokens` can be spent on hidden reasoning.
- The endpoint keeps only the last system message, so the adapter merges the leading run of system
  messages (agent instructions, retrieved evidence, pinned compaction summaries), in order, into
  one. A system message later in the conversation is sent in place as a user message prefixed
  `[system] ` so it keeps its position.
- Embedding models (`gemini-embedding-*`) report `chat: false, embeddings: true`.
- A wrong or revoked key is answered with HTTP 400 `INVALID_ARGUMENT`, reason `API_KEY_INVALID`
  ([Gemini API errors](https://ai.google.dev/gemini-api/docs/generate-content/api-errors)), not
  401. Since 0.2.0b5 the adapter reports it as a non-retryable `authentication` error (keeping
  `status_code` 400); before, it was an `invalid_request`.

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
- Streamed calls map HTTP errors by status as plain calls do (since 0.2.0b5; before, every status
  but 429 was `unavailable`), and an `error` event in the stream (`overloaded_error`,
  `api_error`, ...) fails the call instead of completing it with the text so far; see
  [providers](providers.md#when-the-router-falls-back).
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

## Live provider contracts

The offline contract tests (`tests/contract`) check each adapter against recorded wire shapes.
The live suite (`tests/live`, marker `live`) checks the same adapters against the real vendor
APIs; a maintainer runs it with their own keys before every release (see
[releasing](releasing.md#live-provider-contracts) for the command, variables and cost). It goes
through the Runtime's own provider classes, `ModelRouter`, `build_container` and `Runtime.run`,
never raw HTTP, and proves, per provider and model:

| Check | What a pass proves |
|---|---|
| generate | A plain call answers; `finish_reason` is `stop`; usage has non-zero input and output tokens; the router prices the call and the usage meter is charged exactly the reported usage. |
| stream | The deltas assemble to the final text; a streamed call reports usage (`stream_options.include_usage` is sent where `stream_usage` is on); the meter is charged that usage, not an estimate. |
| usage parity | The same prompt streamed and not streamed counts the same input tokens (within 10%). |
| tools | A tool call comes back with the Runtime name (the wire alias `live__get_forecast` maps back to `live.get_forecast`) and JSON-object arguments; the tool result round-trips to a final answer that quotes it. Plain and streamed. |
| json schema | With `json_schema`, a `response_schema` (including constraints the wire format drops) yields JSON valid against the full schema; with only `structured_output`, the answer parses as JSON with the requested fields. Plain and streamed. |
| images | An inline `ImageBlock` and an attached image (`FileBlock`) are both read (a red square is called red). |
| pdf | An attached one-page PDF is read (its code is quoted back). |
| embeddings | `embed` returns one non-zero vector per text, of equal length. |
| wrong key | A key the vendor never issued is a non-retryable `authentication` error with HTTP 401 (Gemini: HTTP 400 `API_KEY_INVALID`), plain and streamed, also through the router; the error text never quotes the key. |
| unknown model | A model (or Azure deployment) that does not exist is a non-retryable `invalid_request` error with HTTP 404 or 400, plain and streamed. |
| cancel | Cancelling a live stream, or closing it early, closes the HTTP response within two seconds, leaves no task behind, and charges the meter an estimate for the output already delivered. |
| agent run | An agent built from configuration (`build_container`, the provider's default endpoint and `env://` key) completes a run that calls a tool and quotes its result, plain and streamed; the meter saw every model call with the usage the run counted. |

A capability check runs only when the provider class claims the capability for the model
(`capabilities(model)`), and is skipped otherwise. So a capability this page or the class claims
but the live API does not honour fails the suite. Known provider bugs are expected failures
(`xfail`, strict) until they are fixed.

`ALGEN_LIVE_FAKE=1` runs the suite against a local fake of each vendor's dialect instead. It
proves the suite and the request shapes, not the vendors.

### Results

Fill in a row block per run (paste the table `ALGEN_LIVE_REPORT` writes). `pass`, `FAIL`,
`xfail` (known bug), `skip` (not claimed, or not run), `-` (no such check).

| Date | Mode | Runtime | Providers run | Models | Result |
|---|---|---|---|---|---|
| 2026-11-16 | fake (`ALGEN_LIVE_FAKE=1`) | 0.2.0b5 branch | all ten | defaults | 157 passed, 20 skipped (not claimed), 3 xfailed (known bugs below) |
| 2026-11-16 | fake (`ALGEN_LIVE_FAKE=1`) | 0.2.0b5 branch, with the fixes below | all ten | defaults | 160 passed, 20 skipped (not claimed), 0 xfailed |
| _to be filled_ | live (`ALGEN_LIVE=1`) | 0.2.0b5 | | | |

No known bug is marked as an expected failure. The ones the suite found first are fixed in
0.2.0b5, each with a regression test in `tests/contract/test_provider_error_contracts.py`:
streamed Anthropic HTTP errors mapped by status, errors reported inside a stream (Anthropic and
OpenAI-style) failing the call, Azure OpenAI embeddings sent to the embeddings deployment with
`api-version`, `embed` timeouts and transport errors, plain-call `cached_tokens`, DeepSeek JSON
mode, and Gemini's `API_KEY_INVALID` as `authentication`.

## Capability narrowing semantics

Deployments configure provider instances under arbitrary mapping keys in `agent.yaml`. An optional `capabilities:` declaration in configuration applies uniform capability narrowing across all adapter types:

$$C_{effective}(m) = C_{adapter}(m) \cap C_{configured}$$

- **Narrowing:** If an adapter or model reports support (`true`), configuration can restrict it (`false`).
- **No widening:** If an adapter or model reports unsupported (`false`), configuration `true` is ignored and remains `false`, emitting a diagnostic warning.
- **Discovery preservation:** Omitting the `capabilities` block, or omitting individual fields in a partial `capabilities` block, preserves adapter discovery: omitted fields inherit adapter-reported capabilities rather than being set to false.


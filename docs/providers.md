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

`MistralProvider` is an example of a dedicated compatible adapter: it reuses normalized chat, tool, streaming, structured-output, and embedding handling while translating Mistral-specific multimodal image blocks. Configure it with `type: mistral` and an `env://MISTRAL_API_KEY` secret reference. Capability declarations can be narrowed in YAML for the selected Mistral model.

Secrets are references (`env://NAME`) resolved immediately before a request. Never accept or log literal secret configuration.

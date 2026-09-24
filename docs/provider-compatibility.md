# Model provider compatibility

Capabilities depend on the selected model and server. These adapter defaults are conservative; deployments may narrow generic endpoint declarations.

| Adapter | Text/chat | Stream | Tools | JSON schema | Embeddings | Images | Endpoint style |
|---|---:|---:|---:|---:|---:|---:|---|
| OpenAI | yes | yes | yes | yes | yes | yes | OpenAI |
| Azure OpenAI | yes | yes | yes | yes | yes | model-dependent | Azure OpenAI |
| Anthropic | yes | yes | yes | no native schema guarantee | no | yes | Messages |
| DeepSeek | yes | yes | model-dependent | no strict guarantee | endpoint-dependent | no | OpenAI-compatible |
| Mistral AI | yes | yes | model-dependent | model-dependent | yes | model-dependent | Mistral Chat/Embeddings |
| Ollama | yes | yes | model-dependent | model-dependent | yes | model-dependent | OpenAI-compatible |
| Hugging Face Inference | yes | yes | model-dependent | no guarantee | endpoint-dependent | model-dependent | OpenAI-compatible router |
| Local Transformers | yes | completion only | no | no | no | no | in-process |
| Generic OpenAI-compatible | configured | configured | configured | configured | configured | configured | OpenAI-compatible |
| Mock | yes | yes | yes | yes | yes | yes | deterministic in-process |

An unsupported requested feature fails with `CapabilityError` before provider invocation.

## Capability narrowing semantics

Deployments configure provider instances under arbitrary mapping keys in `agent.yaml`. An optional `capabilities:` declaration in configuration applies uniform capability narrowing across all adapter types:

$$C_{effective}(m) = C_{adapter}(m) \cap C_{configured}$$

- **Narrowing:** If an adapter or model reports support (`true`), configuration can restrict it (`false`).
- **No widening:** If an adapter or model reports unsupported (`false`), configuration `true` is ignored and remains `false`, emitting a diagnostic warning.
- **Discovery preservation:** Omitting the `capabilities` block, or omitting individual fields in a partial `capabilities` block, preserves adapter discovery: omitted fields inherit adapter-reported capabilities rather than being set to false.


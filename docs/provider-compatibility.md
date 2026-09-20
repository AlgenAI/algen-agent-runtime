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

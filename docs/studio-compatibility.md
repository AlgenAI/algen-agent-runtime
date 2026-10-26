# Studio compatibility

Studio reads a project's declared settings and nothing else: it builds its own container from
`agent.yaml` and never runs the project's entry point, server or workers. A project imports cleanly
when every component its agents name is declared, and `algen-agent-runtime check` reports what is
not.

## What Studio can see

| Declared in | Gives Studio |
| --- | --- |
| `providers`, agent `default_model` / `fallback_models` | model providers and their `env://` secrets |
| `http_tools` | HTTP API tools ([HTTP API tools](http-tools.md)) |
| `mcp_servers` | MCP servers; their tools appear when Studio connects ([MCP](mcp.md)) |
| `retrieval` | retrievers and their `retrieval.<name>` context builders ([RAG](rag.md)) |
| `domain_plugins` | every code component a plugin registers ([plugins](plugins.md)) |
| `PluginManifest.env`, `Toolkit(env=...)`, `env://` references | the environment variables a deploy asks for |
| `workflows` | workflow manifests and their hook providers ([workflows](multi-agent-workflows.md)) |

`algen-agent-runtime describe --json` lists what a plugin registers without calling any component.
Studio is moving to run it in its build sandbox and check names against that inventory; until it
does, Studio checks names against its own list and may still report a plugin's components as
unknown.

## What Studio cannot see

| In code | Studio reports | Move it to |
| --- | --- | --- |
| `build_container(additional_tools=..., mcp_servers=..., framework_adapters=...)` | the agent's names as unknown | a plugin |
| `container.tools.register(...)`, `register_starter_tools(container.tools)`, `router.register_provider(...)` | the agent's names as unknown | a plugin |
| `container.runtime.policies.register(...)`, `container.analytical_graphs.register_handler(...)`, `container.retrievers.register(...)` | nothing, or the agent's names as unknown | a plugin (`policies`, `analytical_nodes`, `retrievers`) |
| `os.environ[...]`, `os.getenv(...)` | nothing; deploy never asks for the variable | an `env://` reference, or `PluginManifest.env` / `Toolkit(env=...)` |
| `AgentDefinition(...)` in code | nothing; the agent does not exist in Studio | `agents` in `agent.yaml` |
| the project's own server, workers or approval flow | nothing; a Studio test does not run them | keep them, and know that Studio tests only the Runtime contract |

## Check before import

```bash
algen-agent-runtime check agent.yaml --path src
```

Errors (an unknown name, an untrusted plugin, a conflict) are what Studio would reject. Each warning
names code Studio will not run; `OWN_CONTAINER` is expected for any project with a runnable entry
point, the others are worth fixing. `--json` prints the inventory Studio will build from. See
[plugins and the component inventory](plugins.md) for the issue codes.

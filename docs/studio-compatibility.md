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

`algen-agent-runtime describe --json` (since `0.1.0a11`) lists what a plugin registers without
calling any component. Studio runs it, with only the plugin declarations, inside the image each
project build makes (no network, read-only image, unprivileged user) and checks the names agents
use against that inventory, the Runtime's own components and Studio's starter tools. Until a build
has described the declared plugins, Studio reports their components as waiting for a build
(`COMPONENT_PENDING_BUILD`), not as unknown; a plugin that fails to load is reported as
`PLUGIN_LOAD_FAILED`, the same error `algen-agent-runtime check` shows locally.

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

`check` uses the `studio` profile by default. A project that runs its own host and is not
imported into Studio can use `--profile standalone` (since `0.1.0a21`), which leaves out the
warnings about code Studio cannot run. Studio shows the same code findings (`OWN_CONTAINER`,
`CONSTRUCTOR_COMPONENTS`, `POST_BUILD_REGISTRATION`, `CODE_DEFINED_AGENT`, `DIRECT_ENV_READ`) when a
project is imported.

## Runtime versions

Studio pins one exact Runtime release for its images and its exported projects' dependency range
(checked 2026-11-16: Studio's `main` runs `0.2.0b3`, supported range `>=0.1.0a23,<0.3`). A project
imported into Studio runs on Studio's Runtime, not on the version its own lock file names; Studio
replaces a locked Runtime pin with its own when it knows that release's hashes. Check a project
with the Runtime version Studio runs (`pip install "algen-agent-runtime==<version>"`).

These `0.2.0b4` changes decide whether a project that ran on `0.2.0b3` still runs as before; check
them before Studio moves to `0.2.0b4` or later:

| Change in `0.2.0b4` | What a project declares |
| --- | --- |
| `core.http` refuses every call while `security.allowed_http_hosts` is empty | list the hosts, or set `security.allow_any_public_host: true` |
| `core.subprocess` runs only executables listed in `security.sandbox.allowed_executables` (absolute paths) | list them, as well as `security.allow_subprocess_tools: true` |
| a coordinator must enable every tool its delegate enables (except `core.working_state_update`, `core.skills_load`, `core.context_read`); delegates inherit their parent's approval requirements and guardrails; `runtime.subagent.max_descendants` (default 64) caps a run tree | add the delegate's tools to the coordinator's `enabled_tools`, and expect delegated children to pause for approvals |
| Gemini, xAI, Groq and Mistral providers with a non-vendor `base_url` need an explicit `api_key` | an `env://` reference for that provider's `api_key` |
| a stdio MCP server's credential-named `env` value must be an `env://` reference, as must a retrieval `connection_url` with a password | `env://` references |
| `POST /v1/agents` needs `agents:admin`; artifacts are visible to their owner, the run's user or `artifacts:admin` | for an exported service's tokens: grant `agents:admin` and `artifacts:admin` only to operator tools |

The [CHANGELOG](../CHANGELOG.md) upgrade notes for `0.2.0b4` list every change, and
[upgrading](upgrading.md) covers 0.1 to 0.2.

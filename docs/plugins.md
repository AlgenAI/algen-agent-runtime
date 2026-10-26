# Plugins and the component inventory

A plugin is the declared way to add code components to a container: tools, planners, context
builders, verifiers, response composers, semantic layers, analytical methods, model services, model
providers and MCP servers. An agent names its plugins in `domain_plugins`, so the settings say where
every component comes from, and `algen-agent-runtime describe` lists them all without running any.

## Declare a plugin

```yaml
security:
  trusted_plugin_prefixes: ["crm_agent"]
agents:
  - name: crm-operator
    # ...
    enabled_tools: [crm.find_account, crm.update_owner]
    domain_plugins: ["crm_agent.plugin:CrmPlugin"]
```

```python
from typing import Any

from algen_agent_runtime.plugins.sdk import PluginManifest, plugin


@plugin(
    PluginManifest(
        name="crm-agent.tools",
        version="1.0.0",
        publisher="acme",
        env=frozenset({"CRM_API_TOKEN"}),
    )
)
class CrmPlugin:
    def register(self, context: Any) -> None:
        client = CrmClient(token_reference="env://CRM_API_TOKEN")
        context.add_resource(client)
        context.tools.register(find_account_tool(client))
        context.tools.register(update_owner_tool(client))
```

A `domain_plugins` entry is either `module:attribute` or the name of an installed entry point in the
`algen_agent_runtime.plugins` group. The attribute is a plugin class, a plugin instance or a factory
that returns one. A plugin has a `PluginManifest` and a `register(context)` method.

`build_container` loads each plugin once, even when several agents name it, in declaration order.
The container's own components are registered first, then the plugins, then the constructor
arguments `additional_tools=` and `mcp_servers=`, which keep working for tests and existing
deployments.

## Moving from constructor arguments

A deployment that passes components to `build_container` or registers them on the built container
moves them into one plugin; nothing else changes:

1. Create a module with a plugin class whose `register(context)` does what the deployment did:
   `context.tools.register(tool)` for each of `additional_tools`, `context.mcp_servers.register(config)`
   for each of `mcp_servers`, `context.providers.register(provider, registration_id=...)` for each
   provider registered on the router. Hand shared clients to `context.add_resource`.
2. Name it in every agent that uses its components: `domain_plugins: ["my_agent.plugin:MyPlugin"]`,
   and trust its package: `security.trusted_plugin_prefixes: [my_agent]`.
3. Call `build_container(settings)` without those arguments, and delete the registrations on the
   built container.
4. Run `algen-agent-runtime check agent.yaml --path src`: no `UNKNOWN_COMPONENT`, and no
   `CONSTRUCTOR_COMPONENTS` or `POST_BUILD_REGISTRATION` warnings.

`examples/pattern_text_to_sql` and `examples/case_study_teaching_assistant` were moved this way.
New tools are easier to write with `@tool` and `Toolkit` ([writing agent tools](tool-authoring.md)).

## What a plugin can register

| `context.` | Registers | Name taken from |
| --- | --- | --- |
| `tools` | `Tool` | `definition.name` |
| `planners` | planner | `name` |
| `contexts` | context builder | `name` |
| `verifiers` | verifier | `name` |
| `composers` | response composer | `name` |
| `semantics` | `SemanticLayer` | `definition.name` |
| `methods` | `(AnalyticalMethodManifest, implementation)` | `name@version` |
| `model_services` | `ModelService` | `manifest.name@manifest.version` |
| `providers` | `ModelProvider`, with the `ModelRouter.register_provider` arguments | `registration_id`, else `provider_id` |
| `mcp_servers` | an `MCPServerConfig`; the container connects at start | `name` |
| `policies` | a guardrail policy an agent names in `guardrail_policy.policies` or `tool_policies` | `name` |
| `analytical_nodes` | `(kind, handler)` for an analytical node kind with no handler (`semantic_query`, `model_call`) | the kind |
| `retrievers` | `(name, store, **options)`: a search store and its `retrieval.<name>` context builder | `name` |

`context.secrets` resolves `env://` references with the container's environment (call it when a
component starts or runs, never in `register`), and `context.declare_env(*names)` adds variable
names to the plugin's entry in `describe`. A `Toolkit` uses both.

`context.add_resource(obj)` hands a shared client to the container: its `start()` runs with
`Container.astart()` and its `close()` or `aclose()` with `Container.aclose()`.

A name that is already registered raises `ConflictError`, whether the Runtime, the settings or an
earlier plugin registered it. A plugin cannot replace a built-in such as the `direct` planner or the
`default` composer; give its component its own name and point the agent at it.

`Container.plugins` lists the loaded manifests and `Container.component_sources` records which
plugin registered each component, by kind.

## Startup check

`build_container` fails when an agent names a component that nothing registered, and lists every
such name at once with the closest registered one:

```text
ConfigurationError: unregistered agent components: agent 'crm-operator': enabled_tools
'crm.find_acount' is not registered; did you mean 'crm.find_account'?
```

It checks `enabled_tools`, `planning_strategy`, `response_composer`,
`verification_policy.verifiers`, `context_builder`, `guardrail_policy.policies` and
`guardrail_policy.tool_policies`, and the provider of `default_model` and each `fallback_models`
entry, against everything the Runtime, the settings, the plugins and the
constructor arguments registered. Enabled tools under an MCP server's prefix (`mcp.<server>.` unless
the server sets `prefix`) are checked when `Container.astart()` connects to that server; a server
that fails to connect and is not `required` is recorded in `degraded_dependencies` instead.

An agent with `domain_plugins` may name a model provider that is not under `providers:`, since a
plugin may register it; the startup check fails if none did. Without `domain_plugins`, an unknown
provider is still a settings error.

`runtime.component_check: warn` logs the problems instead of failing. Use it only while moving code
that registers components after `build_container` returns (for example
`register_starter_tools(container.tools)`) into a plugin.

The policy engine skips a guardrail name it does not know, so before the startup check an
unregistered or misspelt policy silently did not run. It now fails the build.

## Trust

Plugins load only from packages listed in `security.trusted_plugin_prefixes`. The whole set of
plugin references is checked before any module is imported; an unlisted one fails the build with a
`ConfigurationError`. Prefixes match on module boundaries: `crm_agent` trusts `crm_agent.plugin`
but not `crm_agent_extra`. Trust is an application-level check, not a sandbox: a trusted plugin runs
in the Runtime's process with its authority. Run untrusted code in an isolated process.

## Secrets

Components read credentials through `env://` references, never `os.environ`. List the variable
names a plugin's components need in `PluginManifest.env`, so a deploy can ask for them.

## Describe the components

`describe_components(settings)` returns the inventory a container built from the settings would
register: every tool with its side effect, idempotency, permissions, timeout, attempts and schemas;
every planner, composer, verifier and context builder; the supported provider types and the
configured providers; MCP servers; semantic layers, analytical methods and model services; and every
environment variable name a deploy must supply.

```bash
algen-agent-runtime describe --config agent.yaml --path src --json
```

Each component's `source` is `runtime` for a built-in, `manifest` for one the settings declare, or
the plugin reference that registered it. `--path` adds a directory, such as `src`, to the import
path for plugins that are not installed. Without `--json` the command prints one line per component.
A configuration error, an untrusted plugin or a name conflict exits with status 1.

## Check a project

`algen-agent-runtime check` validates a project the way Studio will, without building a container:

```bash
algen-agent-runtime check agent.yaml --path src --project-root .
```

It loads the settings (ignoring a top-level `extensions` mapping, where Studio keeps its metadata),
describes the components, and runs the startup check against that inventory. These are errors and
exit with status 1:

| Code | Meaning |
| --- | --- |
| `CONFIGURATION_ERROR` | The settings do not load. |
| `PLUGIN_ERROR` | A declared plugin is untrusted, missing or invalid. |
| `COMPONENT_CONFLICT` | Two sources register the same name. |
| `UNKNOWN_COMPONENT` | An agent names something nothing registered; `suggestion` holds the closest name. |
| `UNSUPPORTED_PROVIDER_TYPE` | A provider's `type` is not one the Runtime builds. |

It also reads the Python under `--project-root` (skipping tests, virtual environments and hidden
directories, and running none of it) and warns about what Studio will not run or cannot see:

| Code | Meaning |
| --- | --- |
| `OWN_CONTAINER` | The project calls `build_container` itself: its entry point, server and workers do not run in Studio. |
| `CONSTRUCTOR_COMPONENTS` | It passes `additional_tools=`, `mcp_servers=` or `framework_adapters=`. |
| `POST_BUILD_REGISTRATION` | It registers tools, planners, providers, policies, search stores, semantic layers or analytical handlers on a built container, or hands `container.tools` to code that does. |
| `DIRECT_ENV_READ` | It reads `os.environ` or `os.getenv`; a deploy asks only for `env://` references and `PluginManifest.env`. |
| `CODE_DEFINED_AGENT` | It builds an `AgentDefinition` in code; Studio sees only agents in the settings. |
| `UNREADABLE_SOURCE` | A file does not parse. |

Calls inside a function named `register`, a plugin's own method, are not flagged. `--json` prints
the report with the full inventory; `--strict` also exits with status 1 on warnings.
`check_project(config_paths, project_root=...)` (`algen_agent_runtime.orchestration.check`) returns
the same `CheckReport`. Every project under `examples/` passes `check` in the test suite.

`AvailableComponents.from_inventory(inventory)` and `unknown_agent_components(agents, available)`
(`algen_agent_runtime.orchestration.validation`) run the startup check against an inventory, so a
tool such as Studio can report unknown names without building a container.

Describe imports the plugin modules and runs their `register(context)`, but it never calls a tool,
planner, provider or other component, resolves no secret and opens no network or database
connection. A plugin's `register` should only construct objects; connect in a resource's `start()`.
MCP tools are discovered when the container connects, so the inventory lists MCP servers, not their
tools.

Code passed to `build_container` as constructor arguments is not part of the settings, so describe
cannot see it. Studio reads only declared settings; a project whose components come from constructor
arguments imports into Studio with `UNKNOWN_*` errors. Move them into a plugin.

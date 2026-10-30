# Agent project shape

A portable project normally contains:

```text
my_agent/
├── __init__.py
├── agent.yaml          # providers, agents, workflows, retrieval, stores, telemetry
├── app.py              # small local entry point using Runtime APIs
├── hooks.py            # only when a workflow needs domain hooks
├── manifest.yaml       # catalogue metadata for examples/marketplace tooling
├── README.md
└── tests/
    └── test_smoke.py
```

Larger applications may put `agent.yaml` under `config/`. Keep the package import path stable so a
workflow `hook_provider: my_agent.hooks:create_hooks` can be loaded after installation.

## Project Scaffolding CLI

Use the built-in CLI to scaffold deterministic, secret-free starter projects:

```bash
# Single governed agent
algen-agent-runtime new my-agent --template agent

# Human-in-the-loop approval workflow
algen-agent-runtime new my-approval --template approval

# DAG workflow with state flow and hooks
algen-agent-runtime new my-workflow --template workflow
```

Generated projects contain an offline mock provider, bounded execution, and zero literal secrets.
Use `--dir <path>` to target a custom folder and `--force` to overwrite existing files.

## Non-Interactive Workflow Execution

For automated testing, CI pipelines, and headless benchmarks, use workflow decision flags:

- `--approve-all`: Automatically approve human checkpoints with default values.
- `--reject-all`: Reject all pending review checkpoints.
- `--answers-file <path.json>`: Provide explicit answers keyed by step ID or prompt substring.

Interactive prompts fail closed with non-zero exit when run without a TTY or on EOF.

## Safe Starter Tools

Instead of writing repetitive ad-hoc local utilities, opt into the bounded starter pack via
`algen_agent_runtime.tools.starter`:

- `starter.calculator`: AST-allowlisted math parser (`ast.parse(mode="eval")`); strictly no `eval()`. Bounded recursion depth and exponents.
- `starter.json_query`: Safe dot/bracket JSON querying and transformation with 1MB input and 256KB output caps.
- `starter.file_read` & `starter.directory_list`: Workspace-rooted text inspection with path traversal (`..`) and symlink-escape denial (`PolicyDeniedError`).
- `starter.clock`: Timezone-aware date/time inspection with pluggable `now_fn` for deterministic tests.

Register starter tools from the project's declared plugin (`for tool in get_starter_tools(...):
context.tools.register(tool)`), so the settings show where they come from. `register_starter_tools`
and `build_container(additional_tools=...)` are for tests and embedding only; Studio cannot see them.
An agent that enables a tool registered only after `build_container` returns fails the startup check.

## Code components

Project tools, planners, context builders, verifiers, composers, providers and MCP servers written
in Python are registered by one plugin module and named in each agent's `domain_plugins`, with the
package listed in `security.trusted_plugin_prefixes`:

```yaml
security:
  trusted_plugin_prefixes: ["my_agent"]
agents:
  - name: my-agent
    enabled_tools: [crm.find_account]
    domain_plugins: ["my_agent.plugin:MyAgentPlugin"]
```

The plugin's `register(context)` only constructs and registers components; clients that connect are
handed to `context.add_resource` and connect on first use or in `start()`.
`algen-agent-runtime describe --path src` lists every component and its source without calling any
of them, and `algen-agent-runtime check agent.yaml --path src` validates the project as Studio will.
`examples/quickstart_agent_tool` is the reference: a `Toolkit` with a read tool and an
approval-gated write tool, registered by its plugin. `algen-agent-runtime new <name>` scaffolds the
same shape (`<name>_plugin.py` plus its `domain_plugins` entry). A project still on constructor
arguments moves with the steps in `docs/plugins.md` ("Moving from constructor arguments").

## Single-agent baseline

- Declare at least one provider and agent in `agent.yaml`.
- Use `env://NAME` for credentials and DSNs.
- Bound `max_steps`, latency, tokens, cost, retries, and memory retention.
- Enable only the tools and permissions the agent needs.
- Choose guardrails, verification, approvals, and model allowlists based on actual risk.
- Use in-memory stores for examples; document PostgreSQL/Redis settings for durable deployments.

Use `examples/quickstart_cloud_chat`, `quickstart_local_chat`, `quickstart_rag`,
`quickstart_conversation`, `pattern_guarded_tools` and `pattern_text_to_sql` as current working
references. Copy concepts, not stale versions of internal classes.

## Tool-heavy and acting agents

An agent with many tools, long runs or writes should turn on the Runtime's guards in `agent.yaml`
instead of telling the model in its instructions (docs/agent-tooling.md, "Tool-call guards"). They
are off by default and keep their state in the run, so it survives an approval pause:

```yaml
agents:
  - name: my-agent
    tool_policy:
      on_error: return_to_model     # refusals and held calls reach the model as notes
      max_refusals_per_tool: 3      # then: stop calling it and tell the user why
      repeat_guard: true            # an identical read with nothing changed returns a note
      max_calls_per_tool:
        crm.search_accounts: 5      # a search the model could keep rephrasing
    wrap_up_policy:
      enabled: true                 # warn when only room to answer is left
    context_policy:
      shorten_older_tool_results: true   # keep old results shortened, not dropped
```

- Arguments are always checked against the tool's input schema before the call runs or is put to
  the user, so keep schemas strict rather than validating again in the handler.
- Give a write tool a `precheck` (`@tool(write(approval=True), precheck=...)`) when it can say why a
  change would fail before anyone is asked to approve it.
- Mark reads of state that changes on its own (a job status, a clock) `volatile=True` (or
  `http_tools[].volatile: true`) so the repeat guard never holds them back.
- A tool that acts as the signed-in user reads `context.credentials` (for example
  `context.credentials["authorization"]`). The host passes them with
  `runtime.run(request, credentials=...)` and again on every `resume`; they are kept in memory only.
  Never put credentials in `RunRequest.metadata`, tool arguments or the agent's instructions:
  metadata is persisted. A run resumed without credentials, or recovered after a restart, gives
  tools an empty mapping, so return an "authentication required" tool error then.
- Usage metering is a deployment setting, not an agent field: the host passes a `UsageMeter` to
  `build_container(usage_meter=...)`, and its `reserve` receives a `MeterCall` naming the tenant,
  user, run and model (docs/agent-tooling.md, "Metering model calls").

## Importability

The project configuration—not Studio metadata—is the source of truth. Browser-folder import uploads
configuration but not executable Python; hook-based projects must also be installed into Studio's
Python environment or imported from a trusted server-visible source. Git imports likewise do not
auto-install untrusted code.

## Exporting hook-backed projects

Studio's standalone and marketplace exports package hook Python only when the project declares it.
List the hook module's own file (`pkg/hooks.py` or `pkg/hooks/__init__.py`) and every package module
it imports under the top-level `extensions.studio.export_assets`. Use a `{source, path}` mapping when
the source lives under `src/`:

```yaml
extensions:
  studio:
    export_assets:
      - {source: src/my_agent/__init__.py, path: my_agent/__init__.py}
      - {source: src/my_agent/hooks.py, path: my_agent/hooks.py}
```

The Studio host must also set `ALGEN_AGENT_STUDIO_EXPORT_ASSET_ROOT` to the directory that the
`source` paths are relative to, and `ALGEN_AGENT_STUDIO_WORKFLOW_HOOK_ALLOWED_MODULES` must cover the
package. Studio reads `extensions` only at import, so re-import after changing it.

`AppSettings` forbids unknown top-level keys, so loading such a file directly through Runtime
fails. Remove `extensions` before `AppSettings.model_validate`, and validate the project that way.



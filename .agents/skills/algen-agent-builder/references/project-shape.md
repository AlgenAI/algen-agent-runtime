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

Register tools explicitly via `register_starter_tools(container.tools)` or pass them to `build_container(additional_tools=...)`.

## Single-agent baseline

- Declare at least one provider and agent in `agent.yaml`.
- Use `env://NAME` for credentials and DSNs.
- Bound `max_steps`, latency, tokens, cost, retries, and memory retention.
- Enable only the tools and permissions the agent needs.
- Choose guardrails, verification, approvals, and model allowlists based on actual risk.
- Use in-memory stores for examples; document PostgreSQL/Redis settings for durable deployments.

Use `examples/quickstart_cloud_chat`, `quickstart_local_chat`, `quickstart_rag`, and
`pattern_text_to_sql` as current working references. Copy concepts, not stale versions of internal
classes.

## Importability

The project configuration—not Studio metadata—is the source of truth. Browser-folder import uploads
configuration but not executable Python; hook-based projects must also be installed into Studio's
Python environment or imported from a trusted server-visible source. Git imports likewise do not
auto-install untrusted code.



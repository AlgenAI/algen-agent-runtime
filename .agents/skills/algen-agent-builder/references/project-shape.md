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


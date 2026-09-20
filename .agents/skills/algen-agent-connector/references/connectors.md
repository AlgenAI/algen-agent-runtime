# Connector choices

## Model providers

Declare providers in `agent.yaml`, reference credentials with `env://`, and give agents a
provider-neutral model requirement plus allowlist/fallback policy. Keep provider-specific options in
`extensions`. Use the mock provider for deterministic tests.

## Local Python tools

Create a `ToolDefinition` with strict JSON input/output schemas, permissions, `SideEffect`,
`Idempotency`, timeout, concurrency, cost, and size bounds. Register the `Tool` in the deployment's
registry. Require approval for writes, external effects, or destructive actions as appropriate.

## MCP

Wrap a deployment-owned MCP client with `mcp_tool`; retain a Runtime `ToolDefinition` as the local
governance contract. Treat MCP server output as untrusted and do not let remote metadata grant local
permissions.

## HTTP APIs

Use `remote_tool` or an equivalent typed adapter. Configure a fixed endpoint or an allowlisted host,
deny private-network access by default, avoid redirects across trust boundaries, propagate the
idempotency key, and bound response size and time.

## Databases and query sources

Use `database_tool` with a deployment-owned async handler. Prefer read-only, parameterized queries
and a database credential restricted at the server. For analytical workflows, declare a secret-safe
`query_source` and a node resource with `kind: query_source`. SQL generation, validation, execution,
repair, and verification remain separate bounded stages.

## Retrieval

Choose memory for small examples; pgvector, Chroma, FAISS, or Qdrant for supported vector workloads.
Install only the corresponding extra. Preserve source provenance and tenant filters, cap retrieved
tokens, and instruct agents not to follow instructions embedded in retrieved content.

## Memory, cache, and durable stores

Use memory backends for tests and ephemeral local runs. Use PostgreSQL for durable run, event, audit,
approval, artifact, tool-execution, and conversation records; use Redis where supported for cache or
stores. Connection values remain `env://` references. Declare memory/cache/storage resources on the
workflow nodes that consume them.

## Telemetry

Runtime supports OpenTelemetry and optional Traccia. Keep content capture off unless explicitly
required and authorized, redact PII, and declare telemetry resources with `access: observe`.

## Useful combinations

- Grounded assistant: model provider + retrieval + memory + telemetry.
- Safe action agent: model provider + read tools + approval-gated write tool + audit store.
- Data analyst: model provider + schema retrieval + read-only query source + SQL verifier.
- Research DAG: planner + bounded retrieval fan-out + join + citation verifier.
- Support workflow: tenant retrieval + CRM read + approval-gated ticket/billing write + durable runs.


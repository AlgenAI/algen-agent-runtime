# Connector choices

## Model providers

Declare providers in `agent.yaml`, reference credentials with `env://`, and give agents a
provider-neutral model requirement plus allowlist/fallback policy. Keep provider-specific options in
`extensions`. Use the mock provider for deterministic tests. A provider built in code is registered
from a declared plugin (`context.providers.register(provider, registration_id=...)`), not with
`router.register_provider` on a built container.

## Local Python tools

Write each tool as a typed function with a docstring and `@tool` or `Toolkit.tool`
(`algen_agent_runtime.tools.authoring`; see `docs/tool-authoring.md`), choosing the preset from
what the tool does: `read()`, `write(approval=...)`, `external(approval=...)`, `destructive()`, or
`long_running(...)`. Never hand-pick side effect, permission, idempotency and retries separately;
override a preset field with `.replace(...)` only for a stated reason. Use `approval=True` for
writes a person must confirm, or an `ApprovalHandler` when the deployment has its own approval
flow. Group tools that share a client and `env://` secrets in a `Toolkit` whose client factory
resolves the secrets and connects; nothing connects at import or in `register`.

Register the toolkit from a declared plugin (`toolkit.register(context)`, the plugin named in
`domain_plugins`; see `docs/plugins.md`) so Studio and `algen-agent-runtime describe` can see it.
Test every tool with `assert_tool_contract` and call it with `fake_tool_context`
(`algen_agent_runtime.tools.testing`). `examples/quickstart_agent_tool` is the reference. A
hand-built `ToolDefinition` remains valid when a tool needs something the presets cannot express.

For common bounded utilities, prefer the safe starter tool pack (`algen_agent_runtime.tools.starter`)
which includes `starter.calculator` (AST allowlist), `starter.json_query` (JSON path extraction),
`starter.file_read` and `starter.directory_list` (workspace containment and symlink-escape denial),
and `starter.clock` (timezone-aware with deterministic testing hook).

## MCP (Model Context Protocol)

Runtime provides first-class client connectivity through `algen_agent_runtime.mcp` (optional extra
`algen-agent-runtime[mcp]`):

- Supported transports: `stdio` (subprocess execution) and `sse` (remote HTTP stream).
- Server configuration: `MCPStdioServerConfig` and `MCPSseServerConfig`.
- Automated discovery and schema conversion from MCP tool descriptors into Runtime `ToolDefinition`.
- Namespaced registration: `mcp.<server_name>.<tool_name>`.
- Security boundaries:
  - Child environment sanitization (`SAFE_ENV_PASSTHROUGH` allowlist; secrets are never inherited).
  - Explicit tool allowlists per server (`allowed_tools`).
  - Secret reference resolution (`env://`) for remote HTTP/SSE authorization headers.
  - Enforced per-call timeouts (`timeout_seconds`), byte caps (`max_result_bytes`), and redacted error outputs.
- Integration: Declare servers under `mcp_servers` in `agent.yaml`, or register them from a declared
  plugin (`context.mcp_servers.register(config)`), for automated discovery and managed session
  lifecycle (`MCPClientManager` registered in container resources for clean exit).
  `build_container(mcp_servers=...)` remains for tests and embedding; Studio cannot see it.

## HTTP APIs

Use `remote_tool` or an equivalent typed adapter backed by `SafeHttpClient` from
`algen_agent_runtime.security.network`. Configure a fixed endpoint or an allowlisted host
(`allowed_hosts`), deny private/internal networks by default (loopback, RFC 1918, link-local,
carrier-grade NAT, multicast), protect against DNS rebinding via IP-pinned sockets, avoid unvalidated
redirects across trust boundaries, propagate the idempotency key, and bound response size and time.

## Email

Use `communications.email.EmailMessage` and `email_tool` rather than creating an application-local
send contract. Addresses, headers, recipients, subject/body size, attachments, and provider receipts
are validated and content-minimal. Attachments are tenant-authorized Runtime artifact references;
never accept arbitrary filesystem paths. The first adapter, `SmtpEmailSender`, requires TLS by
default, expects credentials resolved by the deployment secret boundary, keeps BCC out of message
headers, and derives a stable Message-ID from the Runtime idempotency key.

Email is an external keyed side effect. Keep Runtime's durable tool-execution ledger enabled in
production. A provider timeout after submission is indeterminate and must be reconciled rather than
automatically replayed. Delivery approval, suppression, bounce/complaint handling, reply ingestion,
provider OAuth, and campaign policy remain application or connector responsibilities.

## Databases and query sources

Use `database_tool` with a deployment-owned async handler. Prefer read-only, parameterized queries
and a database credential restricted at the server. For analytical workflows, declare a secret-safe
`query_source` and a node resource with `kind: query_source`. SQL generation, validation, execution,
repair, and verification remain separate bounded stages.

## Retrieval

Choose memory for small examples; pgvector, Chroma, FAISS, or Qdrant for supported vector workloads.
Install only the corresponding extra. Any other store is registered from the project's plugin with
`context.retrievers.register(name, store, ...)` and named by agents as `retrieval.<name>`. Preserve source provenance and tenant filters, cap retrieved
tokens, and instruct agents not to follow instructions embedded in retrieved content.

## Memory, cache, and durable stores

Use memory backends for tests and ephemeral local runs. Use PostgreSQL for durable run, event, audit,
approval, artifact, tool-execution, and conversation records; use Redis where supported for cache or
stores. Connection values remain `env://` references. Declare memory/cache/storage resources on the
workflow nodes that consume them.

For files and binary connector output, store bytes through `ArtifactStore` and pass only artifact IDs
through messages or workflow values. Preserve tenant scope, checksum, lifecycle state, expiry, and
size limits. User uploads begin `pending_scan`; a deployment-owned scanner marks them `available` or
`quarantined`. PostgreSQL is suitable only for bounded payloads; use the optional S3 adapter for
large blobs while retaining PostgreSQL lifecycle metadata. Configure bucket, prefix, region,
server-side encryption, and optional KMS key—never static access keys. Implement scanners behind
`ArtifactScanner` and invoke `ArtifactLifecycleService` so the pending transition is compare-and-set
and audited; do not update status directly in application domain code.

## Telemetry

Runtime supports OpenTelemetry and optional Traccia. Keep content capture off unless explicitly
required and authorized, redact PII, and declare telemetry resources with `access: observe`.

## Useful combinations

- Grounded assistant: model provider + retrieval + memory + telemetry.
- Safe action agent: model provider + read tools + approval-gated write tool + audit store.
- Data analyst: model provider + schema retrieval + read-only query source + SQL verifier.
- Research DAG: planner + bounded retrieval fan-out + join + citation verifier.
- Support workflow: tenant retrieval + CRM read + approval-gated ticket/billing write + durable runs.

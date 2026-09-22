# Operations guide

Owner: AlgenAI Maintainers  
Status: Active (0.1.x)  
Last reviewed: 2026-09-22  

Run `algen-agent-runtime` with `ALGEN_AGENT_RUNTIME_CONFIG` set to one or more OS-path-separated YAML files. Put secrets in referenced environment variables. PostgreSQL can persist run and multi-agent workflow checkpoints, conversation memory, events, audits, approvals, bounded artifact payloads, analytical graphs, worker leases, and the tool-execution ledger. Redis can alternatively store run checkpoints, expiring conversation memory, and scoped cache entries. See the [artifact lifecycle](artifacts.md), [production-readiness roadmap](production-readiness.md), and [Analytical Runtime](analytical-runtime.md) for their respective contracts and limits.

For large or production artifact payloads, keep lifecycle metadata in PostgreSQL and configure the
S3-compatible adapter documented in [Artifact lifecycle](artifacts.md). Use workload identity rather
than static access keys, require bucket encryption, restrict the role to the configured bucket/prefix,
and grant `artifacts:scan` only to scanner workers or audited operators.

## Durable single-node configuration

Install `algen-agent-runtime[postgres,auth]`, set the referenced secrets, and configure all execution
records on the same PostgreSQL database:

```yaml
storage:
  run_store: postgres
  memory_store: postgres
  event_store: postgres
  audit_store: postgres
  approval_store: postgres
  artifact_store: postgres
  tool_execution_store: postgres
  conversation_store: postgres
  workflow_store: postgres
  postgres_dsn: env://ALGEN_AGENT_RUNTIME_POSTGRES_DSN
  initialize_schema: true
  postgres_min_pool_size: 1
  postgres_max_pool_size: 10
  memory_retention_seconds: 86400
runtime:
  recover_incomplete_runs: true
  recovery_limit: 1000
  shutdown_grace_seconds: 10
conversation_presentation:
  progress_audience: business
  error_audience: business
  show_technical_details: false
  technical_details_expanded: false
analytical_execution:
  graph_store: postgres
query_governance:
  allowed_purposes: [executive_analytics]
  require_certified_sources: true
  maximum_rows: 10000
  maximum_bytes_scanned: 1000000000
  maximum_compute_seconds: 30
distributed_execution:
  enabled: true
  queue_backend: postgres
  lease_seconds: 30
  worker_concurrency: 4
api:
  host: 0.0.0.0
  auth_mode: jwt
  jwt_key: env://ALGEN_AGENT_RUNTIME_JWT_PUBLIC_KEY
  jwt_algorithms: [RS256]
  jwt_issuer: https://identity.example.com/
  jwt_audience: algen-agent-runtime
  tenant_claim: tenant_id
  scopes_claim: scope
  cors_allowed_origins: [https://dashboard.example.com]
  cors_allow_credentials: true
  # Defaults also allow Authorization, Content-Type, Last-Event-ID,
  # X-Tenant-ID, X-User-ID, and X-Scopes.
```

The default API bind is `127.0.0.1`. Development-header authentication has no
implicit scopes and is rejected on a non-loopback host unless
`allow_insecure_development_auth: true` is explicitly configured. That override
is intended only for isolated local/container development networks; public or
shared deployments must use verified JWT authentication or an equivalent
deployment-owned identity boundary.

The schema runner records applied files transactionally. FastAPI startup initializes storage and
recovers non-paused incomplete runs. Completed tool calls return their persisted result; an
interrupted side-effecting call becomes `indeterminate` and requires reconciliation rather than
automatic replay. Approval and clarification waits remain paused across restarts.

Multi-agent workflows use a separate Runtime checkpoint store because their state spans several
child agent runs. Runtime provides `list_recoverable()` and `executor.recover(...)`; the embedding
host must reconstruct the trusted manifest and hook registry during startup. Recovery never guesses
whether an interrupted node is safe: nodes default to `recovery_policy: fail` and must opt into
`retry`. Do not enable retry for an external side effect without stable idempotency and a provider
reconciliation path.

Parent/child composition requires the host to rebuild a `WorkflowRegistry` containing the exact
trusted manifest versions and hook registries before recovery. Each child has its own checkpoint and
inherits tenant, user, conversation, turn, root, and correlation lineage. Runtime rejects recursive
ancestry and excessive nesting. Alerts and support tooling should display both parent and child IDs.

First-class workflow approval nodes persist the proposed secret-free review context and parameters,
expiry, and final attributed decision in that checkpoint store. Configure `workflow_store: postgres`
when approvals must survive process replacement. Runtime fails an expired approval closed when it is
decided or recovered; schedule deployment-owned recovery/maintenance if expiry must take effect while
there is no operator or startup traffic. Alert on approval age and never place secrets or expiring
download URLs in approval state.

For outbound email, use the contract and SMTP setup in [Email](email.md). Resolve SMTP credentials
from the deployment secret manager, require TLS outside explicitly permitted loopback development,
and persist the tool-execution ledger. A transport failure after submission is indeterminate; do not
retry automatically. Reconcile with the provider using the stable Message-ID/idempotency key before
an operator resolves or repeats the send.

Conversation presentation is independent of telemetry. Use `business` for end-user deployments and
`developer` for controlled diagnostic environments. `show_technical_details` governs typed SQL/raw
table disclosures; supporting dashboards should render the Runtime `details` block collapsed when
`technical_details_expanded` is false. These controls do not remove traces, audit events, or internal
logs. Do not let untrusted end users override them without an authorization policy.

Agent identity is stable across definition versions. Runtime spans and Traccia run identity use the
unversioned agent name in `agent.id` and `gen_ai.agent.id`; `agent.version` and
`gen_ai.agent.version` carry the selected version. `agent.definition.id` contains the versioned
`name@version` key for reproducibility. Policies and long-lived dashboards should scope by
`agent.id`, never by `agent.definition.id`.

## Scoped caching

Caching defaults off. Use the in-memory backend for development and Redis for multiple processes or
replicas. Each integration point has an explicit policy and isolation scope:

```yaml
cache:
  backend: redis
  redis_url: env://ALGEN_AGENT_RUNTIME_REDIS_URL
  key_secret: env://ALGEN_AGENT_RUNTIME_CACHE_KEY_SECRET
  key_prefix: algen-agent-runtime:cache
  policies:
    model_capabilities: {enabled: true, scope: global, ttl_seconds: 86400}
    retrieval: {enabled: true, scope: tenant, ttl_seconds: 300, maximum_value_bytes: 2000000}
    query_results: {enabled: true, scope: user, ttl_seconds: 60, maximum_value_bytes: 2000000}
    model_responses: {enabled: false, scope: user, ttl_seconds: 120}
    tool_results: {enabled: true, scope: user, ttl_seconds: 120}
```

Set both referenced values through a secret manager. `key_secret` is optional but recommended; it
HMACs already opaque key material. Tenant, user, session, request content, prompts, SQL, and arguments
never appear in Redis keys or telemetry.

Use tenant scope only when authorization and results are identical for every user in that tenant.
Use user scope for RLS- or role-dependent data, session scope for conversation-dependent outputs, run
scope for repeated work inside one execution, and global scope only for public technical metadata such
as provider capabilities. The authorization fingerprint is included when callers supply one.

Exact model-response caching is intentionally disabled by default because it changes freshness and
sampling behavior. Enable it only for evaluated workloads; streaming and raw-provider responses bypass
it. Tool caching is restricted to idempotent tools classified `none` or `read`. Destructive, write, and
externally visible tools always execute normally. Cached provider usage is reported as cached tokens
with zero incremental configured-provider cost.

Invalidate related entries with `await container.cache.invalidate_tags("dataset:v2")` after a governed
data or configuration release. Prefer a version token in cache material when a deterministic dataset
version is available. Cache errors do not fail agent runs. Alert on cache error rate, hit rate by policy,
oversize rejections, and Redis latency.

Every cache get, set, and invalidation creates OpenTelemetry spans and metrics with backend, policy,
namespace, scope, outcome, duration, and value size only. When Traccia is enabled these signals follow
the same provider/export path as agent and conversation traces; cache content and keys are never sent.

Conversation responses that were in progress during a process restart are marked failed and
retryable; Algen Agent Runtime does not blindly replay a generic conversation handler because the
handler may own non-idempotent external effects. Dashboard clients reload persisted messages and
reconnect to SSE with `Last-Event-ID`. Use fetch-based SSE when bearer authentication is enabled.

Health endpoints are `/health/live` and `/health/ready`. Export OpenTelemetry through a deployment-specific SDK exporter; content capture defaults off. Logs are JSON and redacted. Alert on run failure ratio, provider circuit openings, approval age, p95 model/tool latency, budget exhaustion, and event-store lag.

## Traccia

Traccia is an optional OpenTelemetry-native observability backend. Install it separately so standard OTLP deployments do not carry the dependency:

```bash
pip install -e '.[traccia]'
```

Enable it in YAML:

```yaml
telemetry:
  enabled: true
  service_name: algen-agent-runtime
  trace_level: detailed
  include_content: false
  include_conversation_content: false
  max_content_chars: 16384
  # Optional second exporter for dual delivery to another OTLP backend:
  # otlp_endpoint: https://otel-collector.example.com/v1/traces
  traccia:
    enabled: true
    api_key: env://TRACCIA_API_KEY
    endpoint: https://api.traccia.ai/v2/traces
    metrics_endpoint: https://api.traccia.ai/v2/metrics
    environment: production
    project_id: customer-assistant
    sample_rate: 1.0
    enable_patching: false
    enable_token_counting: true
    enable_costs: true
    enable_metrics: true
    redact_pii: true
    governance_enabled: false
    governance_fail_open: false
    governance_agent_id: my-platform-agent-id
    max_spans_per_second: 100
    flush_timeout_seconds: 5
```

Set `TRACCIA_API_KEY` through the deployment secret manager. Alternatively, omit `api_key` here and let the Traccia SDK load `TRACCIA_API_KEY` or `traccia.toml`. Never put a literal key in YAML.

Algen Agent Runtime initializes Traccia once with `service_role: orchestrator` and `auto_start_trace: false`, scopes each run with its stable logical agent identity, and stops and flushes the SDK during FastAPI shutdown. One `agent.run` root span groups the context, model, tool, verification, and memory spans for an execution. Algen Agent Runtime stamps Traccia-compatible `agent.id`, `agent.name`, `session.id`, tenant, environment, and `llm.usage.*` attributes directly, so identity and provider-reported token usage do not depend on provider SDK auto-instrumentation. Existing spans continue to use the OpenTelemetry API; enabling Traccia changes their provider/export pipeline without coupling orchestration to Traccia SDK types.

Platform policy enforcement is opt-in and separate from Runtime guardrails. When
`governance_enabled` is true, an application composition root should wrap its agent entry point with
the Traccia SDK `govern()` function using `governance_agent_id` and `governance_fail_open`. This checks
the Traccia Platform agent status before the invocation. Algen Agent Runtime does not duplicate or locally
evaluate Traccia Platform spend policies. Its agent token, cost, step, and timeout budgets are local
deterministic termination ceilings.

Each policy boundary emits a clearly named pipeline span such as `agent.policy.input`,
`agent.policy.before_model`, or `agent.policy.after_tool`. The bounded span names share
`policy.operation=agent.policy.evaluate` for aggregation and carry `policy.boundary`,
`policy.subject_type`, and invoked, triggered, and skipped policy counts. Only
policies whose `applies_to` contract includes that boundary are evaluated. Applicable Runtime
guardrails emit child spans with `span.type=guardrail`, `guardrail.name`, `guardrail.category`,
`guardrail.triggered`, `guardrail.enforcement_mode`, `guardrail.boundary`, and reason code. Custom
policies should declare `applies_to: frozenset[PolicyPoint]`; legacy policies without the field remain
applicable to every boundary for compatibility.
With Traccia enabled, the observability adapter creates these spans through the SDK's
`guardrail_span()` helper. The SDK therefore classifies them as high-confidence Tier-A findings and
aggregates `guardrail.summary`, detected categories, missing coverage, and findings onto the root
conversation trace for Guardrail Posture. Conversation and standalone run roots are created through
the Traccia span lifecycle when this adapter is active; a raw OpenTelemetry root bypasses Traccia's
pre-export enrichment processors and cannot receive that summary. Without Traccia, the adapter emits the same portable
OpenTelemetry contract. Detection observes whether a Runtime guardrail ran or fired; it does not
execute or enforce that guardrail.

Conversation handlers follow Traccia's session model: one user message creates one
`conversation.turn` trace, while every turn in that conversation shares the stable conversation ID as
`session.id`. Agent runs started by the handler are child spans in that interaction trace, including
parallel agents, while their own `agent.id` attributes preserve specialist attribution. The turn root
also records `interaction.id`, user/tenant IDs, outcome, child-run count, and content-block count.
This gives the platform both per-turn timelines and session-level duration/token/cost rollups.

`include_conversation_content` controls only the redacted user message and narrative assistant text on
the turn span. It is separate from `include_content`, which controls substantially more sensitive LLM
messages, retrieved context, and tool inputs/outputs. Keep both disabled unless the tenant's data policy
explicitly permits export.

The Traccia adapter registers the SDK's underlying OpenTelemetry provider as the process-global provider before execution. Initialize Traccia before any other component installs a global OTEL provider; conflicting providers are rejected with an actionable configuration error rather than silently dropping spans.

To export the same spans to Traccia and another OTLP backend, also set `telemetry.otlp_endpoint`. Algen Agent Runtime attaches the standard OTLP processor to Traccia's OpenTelemetry provider. Leave it unset when Traccia is the only destination to prevent duplicate delivery.

Automatic library patching defaults off because Algen Agent Runtime already traces normalized model and tool boundaries. Enable it only when deployment plugins make otherwise invisible SDK calls, and review content capture first. `telemetry.include_content` remains false by default. When explicitly enabled, Algen Agent Runtime emits redacted `llm.prompt`, `llm.completion`, normalized model messages, and tool input/output attributes, each bounded by `telemetry.max_content_chars`. `redact_pii` enables Traccia's additional best-effort processor; neither mechanism replaces upstream data-minimization policy.

`telemetry.trace_level` controls runtime span volume. `minimal` keeps conversation, agent-run,
LLM, tool, analytical, and triggered-guardrail spans; `standard` also keeps policy, context,
verification, and memory spans; `detailed` additionally keeps planning, step, cache, and every
guardrail evaluation span. The default is `detailed` for backward compatibility. This setting
changes trace detail, while `sample_rate` independently controls how many complete traces are kept.

Agent, planning, step, context, policy, model, tool, verification, and memory spans carry provider-neutral operational metadata. LLM spans include latency, finish reason, response ID, token-source fields, and configured-rate cost estimates. Pricing and billing fields owned by the Traccia ingestion service are not forged by Algen Agent Runtime.

Environment-only activation is also supported:

```bash
export ALGEN_AGENT_RUNTIME__TELEMETRY__TRACCIA__ENABLED=true
export ALGEN_AGENT_RUNTIME__TELEMETRY__TRACCIA__ENVIRONMENT=production
export TRACCIA_API_KEY='resolved-by-your-secret-manager'
```

Library users should call `await container.astart()` before accepting traffic and
`await container.aclose()` during shutdown. These initialize durable stores, recover ordinary agent
runs, drain active work, close database clients, and flush pending telemetry. Application hosts must
add multi-agent workflow recovery after startup because only the application can safely construct
its trusted workflow hooks.

If runs succeed but do not appear at the ingestion endpoint, first confirm the process executes `container.close()` or FastAPI lifespan shutdown. Set `TRACCIA_DEBUG=true` temporarily to surface exporter diagnostics, verify that the workspace key matches the configured endpoint, and check outbound access to the endpoint. Keep the flush timeout above the deployment's expected exporter latency.

The ordinary agent/conversation background scheduler remains process-local. For analytical graphs,
enqueue a stable deduplication key in `container.work_queue` and run `DistributedWorker` processes
with a registered graph handler. PostgreSQL claims use leases and `SKIP LOCKED`; renew leases for work
that may exceed the configured interval. Graph nodes checkpoint independently, so retry handlers must
resume the graph rather than replay completed side effects. PostgreSQL-backed SSE subscriptions poll
durable history to receive cross-process events. Schema initialization is automatic by default and
can be disabled when an external schema-management process owns it.

Cancellation is cooperative for provider/tool adapters. Keep adapter timeouts lower than run deadlines. Graceful shutdown should stop accepting runs, cancel active tasks, flush telemetry, and leave resumable checkpoints.

## Outbound HTTP and Egress Network Controls

Outbound HTTP requests from tools (`core.http`, `remote_tool`) and remote model services (`HTTPModelService`) are guarded against Server-Side Request Forgery (SSRF) and DNS rebinding:

1. **DNS Rebinding TOCTOU Elimination:**
   - Outbound connections use `create_safe_http_client`, backed by `SafeNetworkBackend`.
   - The runtime resolves destination hostnames once in worker threads, validates every resolved address against SSRF policies, and connects the TCP socket directly to the validated IP. No secondary DNS resolution occurs at the transport layer, eliminating time-of-check-to-time-of-use rebinding windows.
   - For HTTPS destinations, TLS Server Name Indication (SNI) and certificate verification continue to validate against the original requested hostname.

2. **Default Deny Policies:**
   - Private networks (RFC 1918 `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), loopback (`127.0.0.0/8`, `::1`), link-local (`169.254.0.0/16`, `fe80::/10`), carrier-grade NAT (`100.64.0.0/10`), and cloud metadata IP addresses (`169.254.169.254`, `169.254.170.2`, `[fd00:ec2::254]`, `100.100.100.200`) are blocked by default.
   - If a host resolves to multiple IP records (e.g. dual-homed or round-robin), resolution **fails closed** if any record is private or forbidden.
   - Schemes are restricted strictly to `http` and `https`, and URLs with embedded user credentials (`user:pass@`) are denied.

3. **Allowlist Configuration & Overrides:**
   - Configure explicit `allowed_hosts` tuples on tools and model services to restrict outbound destinations to known, approved domains or domain suffixes (e.g. `allowed_hosts=("api.partner.com", ".internal.example.com")`).
   - `allow_private_networks=True` can be passed to tools or `create_safe_http_client` when operating in internal VPCs or running local test harnesses. Never enable this flag on tools exposed to untrusted tenant prompts.

4. **Production Defense in Depth:**
   - Application-layer IP pinning protects runtime execution, but a deployment-level egress firewall (e.g., Kubernetes `NetworkPolicy`, AWS Security Groups) or dedicated forward egress proxy (e.g., Envoy, Squid) remains the recommended defense-in-depth boundary for production deployments.

## API Admission Rate Limiting and Concurrency Quotas

To prevent run, approval, artifact, and conversation floods, the runtime supports tenant-aware admission rate limiting and in-flight concurrency quotas:

```yaml
api:
  rate_limiting:
    enabled: true
    default_rate_per_minute: 120
    default_burst: 30
    max_concurrent_runs_per_tenant: 10
    fail_closed: true
    route_overrides:
      write:
        rate_per_minute: 60
        burst: 10
      run_create:
        rate_per_minute: 30
        burst: 5
        max_concurrent: 5
```

1. **Admission Architecture:**
   - Evaluated by FastAPI admission middleware after client authentication so quotas are strictly keyed by authenticated `tenant_id` rather than spoofable client IPs or forward headers.
   - Uses a token bucket algorithm to support smooth refill while permitting burst capacity up to the configured burst size.
   - Separately enforces in-flight concurrency limits for expensive run creation (`POST /v1/runs`), tracking active invocations and releasing capacity upon run completion, failure, or client disconnection.

2. **Route Classification:**
   - `run_create`: Run creation endpoint (`POST /v1/runs`). Subject to rate limits and `max_concurrent_runs_per_tenant`.
   - `write`: Mutation endpoints (`POST`, `PUT`, `PATCH`, `DELETE`) such as approvals, clarifications, conversation updates, and artifact creation.
   - `read`: Query and inspection endpoints (`GET`).
   - **Exemptions:** Health and readiness endpoints (`/health/live`, `/health/ready`, `/healthz`, `/ready`) and CORS preflight (`OPTIONS`) are strictly exempt and never rate-limited.

3. **HTTP 429 Response Contract:**
   - Rejections return HTTP status code `429 Too Many Requests`.
   - Includes a standard `Retry-After: <seconds>` HTTP response header.
   - Returns a structured JSON payload:
     ```json
     {
       "detail": "Rate limit exceeded for route class 'write'. Please retry after 3 seconds.",
       "code": "RATE_LIMIT_EXCEEDED",
       "retry_after": 3,
       "tenant_id": "tenant-corp",
       "route_class": "write"
     }
     ```
     When in-flight concurrency is exceeded, `code` is `"CONCURRENCY_LIMIT_EXCEEDED"`.

4. **Single-Node vs Multi-Worker Scope:**
   - `InMemoryApiRateLimiter` enforces tenant admission quotas in-process for a single runtime instance.
   - For horizontally scaled multi-worker clusters, configure an edge API gateway or reverse proxy (e.g., Envoy, Kong, Cloudflare, NGINX, AWS API Gateway) to provide cross-replica distributed rate limiting.

Back up each durable store enabled by the application according to its retention policy. User deletion must remove tenant/session memory and authorized artifacts while preserving legally required, redacted audit records.

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
  learned_memory_store: postgres
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
  component_check: error  # or warn; see docs/plugins.md
  recover_incomplete_runs: true
  recovery_limit: 1000
  shutdown_grace_seconds: 10
  recover_service_workflows: true
  readiness:
    model_providers: any        # any | all | none
    require_durable_stores: true
    required: []                # e.g. [mcp.crm, retriever.docs]
    optional: []                # e.g. [cache.redis]
    probe_timeout_seconds: 2
maintenance:
  enabled: true
  interval_seconds: 300
  clarification_timeout_seconds: 604800
  terminal_run_retention_days: 30
  # audit_retention_days: 365  # opt-in; keep unset when audit must be retained indefinitely
conversation_presentation:
  progress_audience: business
  error_audience: business
  show_technical_details: false
  technical_details_expanded: false
conversation_streaming:
  enabled: true          # stream turns whose models can all stream
  delta_interval_ms: 50  # at most one assistant.message.delta per interval per turn
  delta_max_chars: 2048  # largest delta event
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
shared deployments must use verified JWT or OIDC authentication or an equivalent
deployment-owned identity boundary (see [API authentication](#api-authentication)).

The schema runner records applied files transactionally. FastAPI startup initializes storage and
recovers non-paused incomplete runs and the workflow runs the HTTP workflow service started (see
[Readiness and lifecycle](#readiness-and-lifecycle)). Completed tool calls return their persisted
result; an interrupted side-effecting call becomes `indeterminate` and requires reconciliation
rather than automatic replay. Approval and clarification waits remain paused across restarts until a
maintenance sweep expires them (see [Maintenance and data retention](#maintenance-and-data-retention)).

## Readiness and lifecycle

### Readiness contract

`/health/live` answers `{"status": "ok"}` while the process runs. `/health/ready` reports every
dependency the service has and answers:

- **200, `status: ready`** when every dependency is up;
- **200, `status: degraded`** when an optional dependency is down (an optional MCP server that
  failed at startup, a cache, a retriever) but every required one is up;
- **503, `status: degraded`** when a required dependency is down or the model-provider rule is not
  met (`failing` names them);
- **503, `status: draining`** from the moment the service starts to shut down; dependencies are
  not probed then.

```json
{
  "status": "degraded",
  "ready": true,
  "lifecycle": "ready",
  "providers": {"openai": true},
  "stores": {"PostgresDatabase": true},
  "dependencies": {
    "store.run": {"kind": "store", "required": true, "backend": "postgres", "status": "up"},
    "provider.openai": {"kind": "provider", "required": false, "status": "up"},
    "mcp.crm": {"kind": "mcp", "required": false, "status": "down", "reason": "ConnectError"},
    "telemetry.otlp": {"kind": "telemetry", "required": false, "status": "unknown", "reason": "not probeable"}
  },
  "degraded_dependencies": {"mcp.crm": "ConnectError"},
  "failing": []
}
```

`providers` and `stores` keep their earlier meaning (provider health by id, and each
health-capable resource by type name). A dependency's `status` is `up`, `down` or `unknown` (it
cannot be probed; it is reported and never fails readiness). Reasons are content-minimal: an
exception type, `probe failed`, `probe timed out`, `not registered`, or a message of at most 160
characters with URLs, DSNs and credential-like values removed. Every probe is bounded by
`probe_timeout_seconds` and all run concurrently.

| Dependency | Name | Probe | Required by default |
| --- | --- | --- | --- |
| Run, event, approval, audit, artifact, workflow, tool-execution, conversation, memory, learned-memory and analytical-graph stores; the work queue when distributed execution is on; the MCP token store when MCP servers are configured | `store.<kind>` | The backing PostgreSQL pool, Redis client or S3 bucket (one probe per backend); in-memory stores are always up | Yes (`require_durable_stores`) |
| Cache on Redis | `cache.redis` | Redis `PING` | No |
| Model providers | `provider.<id>` | `provider.health()` | See `model_providers` |
| Plugin resources with `health()` or `ping()` | `resource.<TypeName>` | Its `health()`/`ping()` | Yes (`require_durable_stores`) |
| Retrievers | `retriever.<name>` | `health()`/`ping()` when the retriever has one, else `unknown` | No |
| MCP servers | `mcp.<server>` | Discovered at startup, or `down` with the startup error | When `required: true` and not OAuth (OAuth servers connect per tenant or user) |
| OTLP exporter | `telemetry.otlp` | None (`unknown`) | No |
| Traccia | `telemetry.traccia` | The adapter's `health()` if it has one, else `unknown` | No |
| Other startup failures | the `degraded_dependencies` key, for example `workflow.recovery` | — | No |

`runtime.readiness` settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `model_providers` | `any` | `any`: at least one registered provider must be healthy (`failing: ["provider.*"]` otherwise); `all`: every provider is required (the behavior before 0.2.0b1); `none`: providers are only reported. With no providers registered, the rule is met. |
| `require_durable_stores` | `true` | Require every `store.*` and `resource.*` dependency. |
| `required` | `[]` | Further dependencies that must be up. A name that nothing registers is reported `down` with `not registered`. |
| `optional` | `[]` | Dependencies to report without requiring them. A name cannot be both required and optional. |
| `probe_timeout_seconds` | `2` | Upper bound for each probe (0 < value ≤ 30). |

Names are `<kind>.<name>` with kind one of `store`, `cache`, `provider`, `retriever`, `mcp`,
`telemetry` or `resource`; `algen-agent-runtime check` rejects other forms.

### Start and drain

Startup (`Container.astart`, run by the FastAPI lifespan) moves `lifecycle` from `created` to
`starting` to `ready`. It starts resources, discovers MCP tools, recovers incomplete agent runs and
conversations, recovers the workflow service's interrupted runs (below), and starts the background
maintenance loop when `maintenance.enabled`.

Shutdown (`Container.aclose`) moves `lifecycle` to `draining`, which makes `/health/ready` return
503 at once, then in order: stops the maintenance loop; drains the workflow service (running
workflows get `shutdown_grace_seconds` to finish or pause, then are interrupted); drains
conversations and agent runs with the same grace period; closes retrievers and resources; and
moves `lifecycle` to `stopped`. Interrupted agent runs keep a recoverable status, and interrupted
workflow runs keep their last checkpoint (they are not marked `cancelled`); the agent run a
workflow step was waiting on is drained by the runtime, not cancelled by the workflow. Starting or
answering a workflow run while draining is refused with 409.

Configure the orchestrator's termination grace period above `shutdown_grace_seconds` × 3 plus a
margin, so the drain completes before the process is killed.

### Workflow recovery

Runs started through the HTTP workflow API (`POST /v1/workflows/{name}/runs`) record
`owner: workflow_service` in their checkpoint. With `runtime.recover_incomplete_runs` and
`runtime.recover_service_workflows` (both default `true`), startup recovers each such top-level run
that was `pending` or `running` through the engine's `executor.recover`, using:

- the exact configured manifest: same name, version and content fingerprint; a run whose manifest
  is no longer configured or changed is left untouched and logged for an operator;
- hooks loaded only from modules under `security.trusted_plugin_prefixes`
  (`WorkflowHookLoader`); a run whose hooks are not trusted is left untouched;
- the node's `recovery_policy`: interrupted nodes retry only when it is `retry`, and the run fails
  closed otherwise.

Child workflows are recovered through their parent. Waiting runs are not resumed by recovery; the
maintenance sweep expires their waits. Runs that an application drives with its own executor have
no owner and are never touched; such applications keep calling `executor.recover(...)` (and
`executor.expire_pause(...)`) themselves. If recovery fails (for example a hook provider cannot be
imported), the service still starts and readiness reports `workflow.recovery` as degraded.

Runs started before this release have no owner and are not recovered automatically; recover them
once with the executor, or let a decision or answer through the API continue them.

### Workflow events

| Event | When |
| --- | --- |
| `workflow.approval.expired` | An approval wait passed its `expires_at` (by a late decision, recovery or the maintenance sweep); the approval node fails. |
| `workflow.input.expired` | An input (clarification) wait was older than `maintenance.clarification_timeout_seconds` when the sweep ran; the asking node fails with `workflow input expired`. |
| `workflow.recovered` | A recovered run continues after a restart. |

## API authentication

`api.auth_mode` selects how callers are identified:

| Mode | Use | Verification |
| --- | --- | --- |
| `development_headers` (default) | local development only | Trusts `X-Tenant-ID`, `X-User-ID`, `X-Scopes`. Refused on a non-loopback host unless `allow_insecure_development_auth: true`. |
| `jwt` | one signing key you manage | Bearer JWT verified with the static `env://` `jwt_key`, `jwt_algorithms`, `jwt_issuer` and `jwt_audience`; tenant from `tenant_claim`, scopes from `scopes_claim`. Unchanged from earlier releases, except that `none` is refused in `jwt_algorithms`. |
| `oidc` | an OpenID Connect identity provider | Bearer JWT verified against the issuer's published JWKS, with key rotation, claim mapping and the options below. |

`jwt` and `oidc` need the `auth` extra (`PyJWT[crypto]`).

### OIDC issuers and JWKS

```yaml
api:
  host: 0.0.0.0
  auth_mode: oidc
  oidc:
    jwks_cache_ttl_seconds: 300            # how long fetched keys are used
    jwks_min_refresh_interval_seconds: 30  # at most one JWKS fetch per issuer per interval
    http_timeout_seconds: 5
    allow_private_networks: false          # true only for an in-cluster identity provider
    issuers:
      - issuer: https://login.example.com/realms/acme   # exact 'iss'
        # jwks_url: https://login.example.com/realms/acme/protocol/openid-connect/certs
        audiences: [algen-agent-runtime]
        algorithms: [RS256]                # RS*, PS*, ES*, EdDSA only
        leeway_seconds: 30                 # clock skew for exp/nbf/iat, at most 300
        max_token_lifetime_seconds: 900    # optional: refuse long-lived tokens
        required_claims: [azp]             # beyond exp, iss, aud and the subject
        claims:
          subject_claim: sub
          tenant_claim: tenant_id          # or fixed_tenant: acme
          allowed_tenants: []              # optional allowlist
          scope_claims: [scope, scp]
          role_claims: [realm_access.roles, groups]
          role_scopes:
            runtime-operators: [runs:read, runs:write, conversations:read]
            runtime-admins: [auth:revoke]
```

- **Discovery.** Without `jwks_url` the Runtime reads `{issuer}/.well-known/openid-configuration`,
  requires its `issuer` to equal the configured issuer, and fetches its `jwks_uri`.
- **Egress.** Discovery and JWKS fetches use the SSRF-safe client: only the issuer host, the
  `jwks_url` host and the issuer's `additional_allowed_hosts` are reachable; private, loopback,
  link-local and metadata addresses are refused unless `allow_private_networks: true`; redirects
  are not followed; responses are capped at `max_jwks_bytes`; `https` is required unless
  `allow_insecure_http: true` (local test providers only).
- **Caching and rotation.** Keys are cached for `jwks_cache_ttl_seconds`. A token whose `kid` is
  unknown triggers a refresh, rate-limited to one per `jwks_min_refresh_interval_seconds` per
  issuer, so random key ids cannot turn requests into identity-provider traffic. If a refresh
  fails the last good keys stay in use; if keys were never fetched the API answers 503.
  Publish a new key in the JWKS before signing with it, and keep the old key published until the
  tokens it signed have expired.
- **Algorithms.** Only the issuer's `algorithms` are accepted. `none` and `HS*` are refused by
  configuration validation; the token's algorithm must also match the key type (RSA: `RS*`/`PS*`,
  EC: `ES*`, OKP: `EdDSA`) and the key's `alg` when the JWKS states one. Symmetric (`oct`) JWKS
  keys are ignored. Tokens must carry a `kid`.
- **Claims.** `exp`, `iss`, `aud` and the subject are required; `nbf` and `iat` are checked when
  present, all with `leeway_seconds` of skew.
- **Claim mapping.** The subject claim becomes the user id. The tenant comes from `fixed_tenant`
  (one-tenant issuers; takes precedence) or `tenant_claim`; a missing, empty or non-string tenant
  is refused (fail closed). Scopes are the union of the `scope_claims` values (a space-separated
  string or a list) and the scopes `role_scopes` grants for each value in `role_claims`. A `*` in
  a token scope claim is dropped; grant the wildcard through `role_scopes` if you really need it.
  Claim names are looked up literally first (namespaced claims such as
  `https://example.com/tenant`), then as dotted paths (`realm_access.roles`).

### Token revocation

Short-lived access tokens (5 to 15 minutes, enforced with `max_token_lifetime_seconds`) are the
primary revocation control. For a token that must stop working sooner, enable the optional check:

```yaml
api:
  token_revocation:
    enabled: true
    store: postgres     # 'memory' is per-process and lost on restart
    require_jti: false  # true refuses tokens without a jti
```

Each verified `jwt` or `oidc` token is then checked, in one store lookup, against a tenant-scoped
`jti` denylist and a per-subject "revoked before" cutoff (tokens whose `iat` is at or before the
cutoff, or that have no `iat`, are refused). A store outage fails closed with 503. Revoke with
the `auth:revoke` scope, within the caller's own tenant:

- `POST /v1/auth/revocations/tokens` `{"jti", "expires_at", "reason"?}` denies the token until
  its `exp` (the entry is purged afterwards; `expires_at` at most a year ahead).
- `POST /v1/auth/revocations/subjects` `{"subject", "revoked_before"?, "reason"?}` refuses every
  token for the subject issued at or before the cutoff (default now, never more than five
  minutes in the future). A cutoff only moves forward.

Both return 404 while revocation is disabled. From Python, use `container.token_revocations`
(`revoke_token`, `revoke_subject`, `state`, `purge_expired`). The PostgreSQL store uses migration
`013_token_revocation.sql`.

Trade-offs: the check costs one indexed lookup per request and makes the store a dependency of
authentication. OAuth token introspection (RFC 7662) is not implemented: it adds an
identity-provider round trip per request (or a cache that brings back the same staleness), and
short lifetimes plus this denylist cover the common cases. A deployment that needs introspection
can run it in a gateway in front of the Runtime.

### Service identity from a trusted proxy (mTLS)

When a TLS-terminating proxy verifies client certificates, it can pass the client's identity to
the Runtime in a header. Off by default:

```yaml
api:
  trusted_proxy_identity:
    enabled: true
    header: x-client-cert-identity
    trusted_proxies: [10.0.0.0/24]       # CIDRs of the proxies, never 0.0.0.0/0
    identities:
      spiffe://acme/ns/jobs/sa/scheduler:
        tenant_id: acme
        user_id: svc-scheduler           # default: the identity string
        scopes: [runs:write, runs:read]
```

The header is honoured only when the request's direct TCP peer is in `trusted_proxies`, and only
for an exact identity listed in `identities`; any other value is refused. The same header from
any other address is refused (and audited as `untrusted_proxy`) even when the request also carries
a valid bearer token. Requests without the header fall through to bearer authentication. The
hook needs `auth_mode: jwt` or `oidc`.

The proxy must overwrite the header on every request and never pass a client-supplied value
through: for example Envoy `request_headers_to_add` with `%DOWNSTREAM_PEER_URI_SAN%` and
`append_action: OVERWRITE_IF_EXISTS_OR_ADD`, or nginx `ssl_verify_client on` with
`proxy_set_header X-Client-Cert-Identity $ssl_client_s_dn;`. The peer address must be the real
TCP peer: `algen-agent-runtime serve` disables uvicorn's `X-Forwarded-For` rewriting when the
hook is enabled; if you run the app under another server, disable proxy-header rewriting there
too. Terminating mTLS in the Runtime itself (uvicorn `--ssl-cert-reqs`) is not mapped to
principals.

### Authentication audit events

| Kind | Name | Data |
| --- | --- | --- |
| Audit | `auth.authenticate`, outcome `failure` | `method` (`jwt`, `oidc`, `trusted_proxy`), `reason`, and when known `issuer`, `kid`, `subject`. Recorded under the verified tenant when the token was verified (`revoked`, `missing_jti`, `revocation_unavailable`), otherwise under tenant `_authentication`. |
| Audit | `auth.authenticate`, outcome `success` | `method`, `issuer`, `kid`, `subject`; only with `authentication_audit.record_success: true`. |
| Audit | `auth.audit.suppressed`, outcome `suppressed` | `count` of failure records dropped by the per-minute cap. |
| Audit | `auth.token.revoked`, `auth.subject.revoked` (outcome `completed`) | `expires_at` or `revoked_before`, `reason`; the actor is the revoking principal. |
| Metric | `algen_agent_runtime.auth.attempts` (counter) | Attributes `method`, `outcome`, `reason`. |
| Log | `authentication failed` (warning) | `auth_method`, `auth_reason`, `auth_issuer`, `auth_kid`. |

Failure reasons: `missing_credentials`, `malformed_token`, `algorithm_not_allowed`,
`unknown_issuer`, `missing_key_id`, `unknown_key`, `keys_unavailable`, `invalid_signature`,
`expired`, `not_yet_valid`, `invalid_issued_at`, `invalid_audience`, `invalid_issuer`,
`missing_claim`, `token_lifetime_exceeded`, `missing_subject`, `missing_tenant`,
`tenant_not_allowed`, `missing_jti`, `revoked`, `revocation_unavailable`, `untrusted_proxy`,
`invalid_service_identity`, `unknown_service_identity`, `invalid_token`.

No record contains the token, its signature or any claim beyond the subject. A `subject` is
recorded only after the signature is verified (or for a trusted proxy's identity); the `issuer`
and `kid` of an unverified token are attacker-controlled and truncated to 256 characters. Failure
records are capped at `authentication_audit.max_failure_records_per_minute` (default 600) per
process; the excess is counted in one `auth.audit.suppressed` record, and the metric and log
still see every attempt. `authentication_audit.record_failures: false` turns the records off (the
metric and log remain). Each request is authenticated and audited once, even when the rate
limiter authenticates it before the route does. Clients get a generic answer (a rejected token is
`invalid bearer token`, 401 with `WWW-Authenticate: Bearer error="invalid_token"`; unavailable
keys or revocation store are 503); the specific reason is only in the audit.

## Resumable execution budgets

`max_steps`, token, cost, and latency budgets remain important loop and spend controls. For
long-running user goals, enable progressive continuation so a budget boundary becomes a durable
checkpoint instead of a terminal failure:

```yaml
agents:
  - name: operations-agent
    # ...
    max_steps: 24
    budget: {max_tokens: 24000, max_output_tokens: 3000, max_cost_usd: 0.40, max_latency_seconds: 90}
    continuation_policy:
      enabled: true
      automatic_extensions: 1
      step_increment: 24
      token_increment: 8000
      cost_increment_usd: 0.10
      approval_expires_seconds: 1800
```

Runtime grants only the configured automatic extensions. Later boundaries emit the normal
`approval.required` event with `kind: execution_continuation`; approval adds one increment and
resumes the same run, messages, tool receipts, and verification state. This can repeat without an
arbitrary total extension count, but every additional increment remains human-attributed. Rejection
closes the run. Approval and clarification wait time is excluded from the run deadline.

Keep automatic increments small and evaluated. They do not bypass tool policies: external, write,
destructive, and other consequential tools still pause at their own approval boundaries.
Clients should describe saved progress and the next action, not expose internal step counters as the
user-facing answer. Monitor `run.continued`, approval age, total usage, repeated tools, and final
verification outcomes.

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

## Usage metering

A deployment that bills or rations model use passes a `UsageMeter` to
`build_container(settings, usage_meter=...)`. The model router reserves before every provider
dispatch and settles afterwards, so agent steps, retries, fallbacks, schema repairs, streamed calls
and history compaction are all charged; a cached model response makes no call and is not charged.
See [Agent tool behaviour](agent-tooling.md#metering-model-calls) for the protocol and an example.

| Signal | Where | Meaning |
| --- | --- | --- |
| `budget.refused` event | run events | The meter raised `MeterRefusedError`; `data.reason` is its message, `data.provider` the provider registration id and `data.model` the model whose reservation it refused. Never `RunRequest.metadata` values. |
| `run.failed` event with `reason: budget_refused` | run events | The run that the refusal ended; `data.error` repeats the reason, which is also `RunState.error`. |
| `budget.refused` audit record | audit log | Outcome `refused`, the agent as resource, and `reason`, `provider` and `model` as in the event. |
| `budget.refused = true` | `agent.run` span | Set on the run root whose drive the refusal ended. |

A refusal is not a provider error: it is not retried, the router does not try a fallback model, and
it does not count towards the provider's circuit breaker or latency. The run ends `failed`. Alert on
`budget.refused` separately from provider failures. A meter must not raise any other exception from
`reserve` to mean "no": that is treated as an internal error.

## Tool-call guards

The guards in [Agent tool behaviour](agent-tooling.md#tool-call-guards) answer a tool call
without running it, or add a notice to its result. Each leaves a record:

| Guard | Run event | `tool.completed` status | Audit (`action`, `outcome`) | Span |
| --- | --- | --- | --- | --- |
| Schema check | `tool.refused` (`reason_code: tool.schema_invalid`) | `refused` | `tool.call`, `refused` | `agent.tool.call` with `tool.status=refused` |
| Precheck | `tool.refused` (`reason_code: tool.precheck_refused`) | `refused` | `tool.call`, `refused` | `agent.tool.call` with `tool.status=refused` |
| Repeat guard | `tool.repeated` (`reason_code: tool.repeated`) | `repeated` | `tool.call`, `repeated` | `agent.tool.call` with `tool.status=repeated` |
| Call limit | `tool.repeated` (`reason_code: tool.call_limit`) | `over_limit` | `tool.call`, `over_limit` | `agent.tool.call` with `tool.status=over_limit` |
| Wrap-up notice | `tool.wrap_up` (`tool_calls_left`, `reason: steps\|tokens`) | unchanged | `run.wrap_up`, `notified` | `agent.wrap_up.tool_calls_left` on the `agent.step` span (`agent.run` for a parallel reply) |

`tool.refused` also carries `refusal_count` (refusals of that tool so far in the run) and
`refusal_limit_reached` (the note told the model to stop); `tool.repeated` for a call limit
carries `call_limit`. The same values are on the span as `tool.guard.reason_code`,
`tool.guard.refusal_count`, `tool.guard.refusal_limit_reached` and `tool.guard.call_limit`, and in
the audit metadata beside `step_id`. None of them carries argument values. The run narrative lists
the three events. Alert on runs whose `refusal_limit_reached` is true or with many
`tool.repeated` events: the model is stuck on a tool's contract or its instructions.

## Maintenance and data retention

Runtime tables grow with every run. A maintenance sweep keeps them bounded and moves stale paused
runs to a terminal state. Run it inside the API process by setting `maintenance.enabled: true`, or
on a schedule (for example a Kubernetes CronJob) with:

```bash
algen-agent-runtime maintenance --config agent.yaml
```

The command prints a JSON report and exits non-zero if any task failed. Each sweep:

| Task | Default | Behavior |
| --- | --- | --- |
| Paused approvals | on (`expire_paused_runs`) | A run awaiting approval becomes `timed_out` with a `run.failed` event (`reason: approval_expired`) once its approval request has expired or is missing. |
| Paused clarifications | off | With `clarification_timeout_seconds` set, a run awaiting clarification longer than that becomes `timed_out` (`reason: clarification_expired`). |
| Paused workflow waits | on (`expire_paused_workflows`) | For runs the HTTP workflow service started: an approval wait past its `expires_at` fails its approval node (`workflow.approval.expired`), and, with `clarification_timeout_seconds` set, an input wait older than that fails its node (`workflow.input.expired`). The engine's own expiry path does this, so rejection policies, dependents and checkpoints behave as for a late decision, and the webhook is notified of the outcome. Each run is handled in its own tenant; runs already being driven are skipped and saves are version-checked, so repeated or concurrent sweeps expire a wait once. At most `batch_size × max_batches_per_sweep` unfinished runs, oldest first, are looked at per sweep. Reports `expired_workflow_pauses`. Runs only in the serving process (`maintenance.enabled`); the `maintenance` command does not drive workflows. |
| Pending conversation turns | with the paused-run task | A conversation turn whose run the sweep just expired (or that ended otherwise while it waited) ends as `expired`, `cancelled` or `failed`. Reports `reconciled_pending_turns`. |
| Expired memory | on | Deletes PostgreSQL memory rows past `memory_retention_seconds`; readers already ignore them. |
| Expired learned memory | on | Clears content and evidence; retains revisions and replay receipts. Reports `purged_learned_memory`. |
| Expired artifacts | on | Calls `purge_expired()` on the configured artifact store, including S3 blobs. |
| Terminal runs | off | With `terminal_run_retention_days` set, deletes completed, failed, cancelled, and timed-out runs not updated within that window, together with their events, approvals, and tool-execution records. |
| Audit events | off | With `audit_retention_days` set, deletes audit events older than that window. |

Terminal-run deletion is conservative. It keeps runs that still have a `started` or `indeterminate`
tool execution, so operators can reconcile side effects first, and runs that belong to a workflow
that has not finished. Artifacts follow their own expiry; deleting a run clears their `run_id`.
Conversations, workflow checkpoints, analytical graphs, and work items are not deleted by the sweep.

Work is done in batches (`batch_size`, `max_batches_per_sweep`) with `FOR UPDATE SKIP LOCKED`, so
several replicas or a CronJob can sweep concurrently without blocking request traffic or deleting
the same rows twice. Migration `010_retention_indexes.sql` adds the supporting indexes; on large
existing tables, create them `CONCURRENTLY` before upgrading to avoid a long write lock. Choose
retention windows that cover your incident-investigation and compliance needs before enabling
deletion, and back up the database first.

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

### Conversation turns: streaming, approvals and input

A conversation turn is one user message and the assistant message that answers it. The built-in
`runtime` handler runs one agent run per turn and:

- **streams** the run when `conversation_streaming.enabled` is true (the default) and every model
  the agent may use reports streaming. Text deltas reach `GET /v1/conversations/{id}/events` as
  `assistant.message.delta`. A turn sends at most one delta event per `delta_interval_ms` (smaller
  deltas are coalesced), except that a buffer reaching `delta_max_chars` is sent at once; each event
  carries at most `delta_max_chars` characters. Deltas are provisional and may include text the
  model wrote before a tool call: the `assistant.message.completed` message is the record. A turn
  whose models cannot stream produces only the final message. Streamed model calls bypass the
  model-response cache, as streaming runs always have; set `enabled: false` to keep caching turns.
  Delta events carry only model text, never tool arguments or results, and are not audited.
- **pauses** when its run awaits an approval or a clarification. The assistant message moves to
  `awaiting_approval` or `awaiting_input` and its `pending` field (also in the event) carries what a
  UI needs: `turn_id`, `run_id`, `approval_id`, `tool`, `proposed_action`, `side_effect_summary`,
  `risk`, `expires_at`, `options`, `redacted_parameters` (as in the run's `approval.required`
  event; never raw arguments) and the tool's `parameters_schema`, or the `question` to answer.
- **resumes** into the same assistant message when the user answers:

  | Route | Body | Scope |
  | --- | --- | --- |
  | `POST /v1/conversations/{id}/turns/{turn_id}/approval` | `{"decision": "approved" \| "rejected" \| "modified", "modified_parameters": {...}, "reason": "..."}` | `conversations:write` |
  | `POST /v1/conversations/{id}/turns/{turn_id}/input` | `{"text": "..."}` | `conversations:write` |

  `turn_id` is the user message id (`pending.turn_id`). The authenticated user is recorded as
  `decided_by`. Both return `202` with the assistant message id and `streaming`; another tenant or
  user gets `404`; an answer of the wrong kind, a second answer, or an answer to a turn that no
  longer waits gets `409`; an answer after the approval expired ends the turn as `expired` and
  gets `409`. In Python, call `ConversationService.respond(conversation_id, turn_id, tenant_id,
  user_id, ConversationApprovalDecision(...) | ConversationInputAnswer(...), credentials=...)`.

Only one turn per conversation can wait. A new message while one waits is refused with `409`
naming the waiting turn id; answer it or cancel it with `DELETE .../active-response`, which also
cancels the paused run. Deleting the conversation cancels it too.

Each pending turn is stored on its assistant message, so it survives a restart: `recover()` leaves
it waiting and the turn resumes after the restart. If the run was resumed but the process stopped
before the message was updated, recovery follows the run to its end instead of running it again; if
the run ended while the turn waited, the turn ends with that outcome. The maintenance sweep does the
same for turns whose paused run it expires.

`submit(..., credentials=...)` and `respond(..., credentials=...)` pass the caller's credentials to
the turn's run for its tools (see [tool authoring](tool-authoring.md#credentials-of-the-caller)).
They are held in memory only, never stored, emitted, audited or logged; a turn that pauses drops
them, so pass them again when answering. The HTTP routes never forward request headers as
credentials. A custom handler takes them by accepting a `credentials` keyword argument in `handle`;
passing credentials to a handler that does not is a `ConfigurationError`.

| Message status | Meaning | Event |
| --- | --- | --- |
| `accepted`, `streaming` | queued, running (also after an answer) | `assistant.message.started`, `assistant.message.delta`, `assistant.message.resumed` |
| `awaiting_approval` | the run waits for an approval decision | `assistant.message.awaiting_approval` |
| `awaiting_input` | the run asked the user a question | `assistant.message.awaiting_input` |
| `completed` | answered | `assistant.message.completed` |
| `failed` | failed, or the action was rejected (`metadata.reason: approval_rejected`) | `assistant.message.failed` |
| `cancelled` | cancelled by the user or with its run | `assistant.message.cancelled` |
| `expired` | the approval or answer did not come in time | `assistant.message.expired` |

`assistant.message.delta` data is `{message_id, delta}`. `assistant.message.resumed` data is
`{turn_id, kind, decided_by}` plus `approval_id` and `decision` for approvals or the answer `text`
for input. The awaiting, completed, failed and expired events carry the assistant message, with
`turn_id` on awaiting events. A resumed turn opens a new `conversation.turn` span with
`conversation.turn.resumed=true`; its agent run continues the run's original trace.

Health endpoints are `/health/live` and `/health/ready` (see [Readiness and lifecycle](#readiness-and-lifecycle)). Export OpenTelemetry through a deployment-specific SDK exporter; content capture defaults off. Logs are JSON and redacted. Alert on run failure ratio, provider circuit openings, approval age, p95 model/tool latency, budget exhaustion, and event-store lag.

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
    export_whole_runs: true        # send each run's spans together (default)
    run_export_max_spans: 2000     # flush a run early past this many spans
    run_export_max_seconds: 300    # or once it has been open this long
    export_foreign_traces: false   # send only traces the Runtime started (default)
```

Set `TRACCIA_API_KEY` through the deployment secret manager. Alternatively, omit `api_key` here and let the Traccia SDK load `TRACCIA_API_KEY` or `traccia.toml`. Never put a literal key in YAML.

Algen Agent Runtime initializes Traccia once with `service_role: orchestrator` and `auto_start_trace: false`, scopes each run with its stable logical agent identity, and stops and flushes the SDK during FastAPI shutdown. One `agent.run` root span groups the context, model, tool, verification, and memory spans for an execution. Algen Agent Runtime stamps Traccia-compatible `agent.id`, `agent.name`, `session.id`, tenant, environment, and `llm.usage.*` attributes directly, so identity and provider-reported token usage do not depend on provider SDK auto-instrumentation. With Traccia enabled, every Runtime span (run, step, planning, context, policy, model, tool,
verification, memory and conversation spans) is created through Traccia's tracer via the
observability adapter's `tracer()`, so the SDK's enrichment processors (agent identity, token
counting, cost annotation, guardrail detection, governance) see all of them; orchestration code still
depends only on the adapter, not on Traccia types. Without Traccia the same spans use the
OpenTelemetry API.

Every Runtime span carries a `span.type`: `agent`, `step`, `planning`, `context`, `policy_pipeline`,
`guardrail`, `LLM`, `tool`, `verification`, `memory` or `cache`.

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

**Whole-run export.** OpenTelemetry's batch processor exports whatever spans have ended each time its
timer fires, so a run used to leave in several batches with its `agent.run` root, which ends last, in
the final one; a backend that lost or rejected one batch showed a run with a hole, or child spans whose
parent never arrived. With `export_whole_runs` (the default), the adapter puts a
`RunScopedExportProcessor` in front of the export processors present when Traccia starts: it holds a
trace's spans until the span that opened it in this process ends, then hands them over together. A
run past `run_export_max_spans` spans or open longer than `run_export_max_seconds` is flushed early, and
the rest follows its root. The root records `agent.run.span_count`, the number of spans the run
emitted, so a backend can tell a whole trace from a partial one. Other OpenTelemetry deployments can
use `install_run_scoped_export(provider)` from `algen_agent_runtime.observability.run_export`.

**Foreign traces.** Traccia's provider is the process-global one, so a library that traces through
the global provider (FastAPI's request tracing, an httpx or SQLAlchemy instrumentor) reaches the same
exporters, with no agent identity; Traccia files those spans under `unknown-agent-name`. With
`export_foreign_traces: false` (the default), the adapter puts a `RuntimeTraceFilterProcessor` in front
of the export processors present when Traccia starts (in front of the whole-run processor when that is
on): a trace is exported only when the span that opened it in this process is a Runtime root, and
every other trace is dropped whole, including spans that end after its root. A Runtime root is either:

- a span carrying `algen.runtime.root=true`: `conversation.turn`, an `agent.run` that opens its trace
  (a standalone run, or a resumed run under its stored parent; a run inside a turn is the turn's
  child and is not marked), or
- a span created by a Runtime tracer (instrumentation scope `algen_agent_runtime.*`), so Runtime spans
  that start without a run, such as `model_service.invoke`, `conversation.feedback` or
  `distributed_work.execute`, keep reaching Traccia.

A run started while an HTTP request span is current, as when a FastAPI handler starts it, opens its
own trace instead of becoming the request's child, so it is exported whole and not dropped with the
request. A Runtime component span created inside an application's request span, outside any run,
belongs to the request's trace and is dropped with it. Set `export_foreign_traces: true`
(`ALGEN_AGENT_RUNTIME__TELEMETRY__TRACCIA__EXPORT_FOREIGN_TRACES=true`) to send everything as before,
with runs as children of the request that started them, when an application wants its own spans in
Traccia. An exporter added after Traccia starts, such as `telemetry.otlp_endpoint`, is not filtered.
Other OpenTelemetry deployments can use `install_runtime_trace_filter(provider)` from
`algen_agent_runtime.observability.runtime_traces`.

**Resumed runs.** A run stores its first `agent.run` span in `RunState.trace_parent`; when it resumes
after an approval or a pause, the new `agent.run` is a child of that span, so the whole run is one
trace even across processes.

To export the same spans to Traccia and another OTLP backend, also set `telemetry.otlp_endpoint`. Algen Agent Runtime attaches the standard OTLP processor to Traccia's OpenTelemetry provider. Leave it unset when Traccia is the only destination to prevent duplicate delivery.

Automatic library patching defaults off because Algen Agent Runtime already traces normalized model and tool boundaries. Enable it only when deployment plugins make otherwise invisible SDK calls, and review content capture first. `telemetry.include_content` remains false by default. When explicitly enabled, Algen Agent Runtime emits redacted `llm.prompt`, `llm.completion`, normalized model messages, and tool input/output attributes, each bounded by `telemetry.max_content_chars`. `redact_pii` enables Traccia's additional best-effort processor; neither mechanism replaces upstream data-minimization policy.

`telemetry.trace_level` controls runtime span volume. `minimal` keeps the decision chain: the
conversation turn and agent run, each step, model and tool calls, one `agent.policy.<checkpoint>`
summary per checkpoint (policies invoked, triggered and skipped), verification, analytical spans and
guardrails that fired. `standard` adds context building and memory writes; `detailed` adds planning,
cache operations and every guardrail evaluation, fired or not. The tool executor's policy spans
follow the same level. The default is `detailed` for backward compatibility. This setting
changes trace detail, while `sample_rate` independently controls how many complete traces are kept.

Agent, planning, step, context, policy, model, tool, verification, and memory spans carry provider-neutral operational metadata. Agent identity is on every span; descriptive attributes (description, tags, capabilities, planning strategy, run mode) are on the run root only, and empty values are left out.

What each span records without any content (`include_content` adds prompts, answers and tool payloads):

| Span | Analysis signals |
| --- | --- |
| `agent.run` | status, outcome, step, model, tool and retry counts, input and output tokens, `agent.run.input_chars`, `agent.run.output_chars`, `agent.run.span_count`, and a cost only when provider rates priced the run |
| `agent.model.call` | model, finish reason, latency, `llm.time_to_first_token_ms` on streamed calls, `agent.model.prepare_ms` (policy checks and event delivery before the call), message and tool counts sent, `llm.response.tool_names` requested, `llm.response.output_chars` |
| `agent.tool.call` | tool, side effect, idempotency, `tool.argument_keys`, status, `error.type` on failure, `tool.result_chars` |
| `agent.context.build` | builder, message count, retrieved source IDs and `retrieval.scores` |

Tokens follow both conventions without duplicates: `llm.usage.prompt_tokens` is the uncached input,
beside `llm.usage.cache_read_tokens` and `llm.usage.cache_write_tokens`, as Traccia prices them;
`gen_ai.usage.input_tokens` is the whole prompt, as OpenTelemetry's GenAI conventions define it, with
`gen_ai.usage.cache_read.input_tokens` and `gen_ai.usage.cache_creation.input_tokens`. A model span
carries `llm.cost.usd` only when configured provider rates priced the call; an unpriced call has no
cost rather than 0.0, so Traccia's cost processor prices it from its own table. Pricing and billing
fields owned by the Traccia ingestion service are not forged by Algen Agent Runtime.

**Run narrative.** `GET /v1/runs/{run_id}/narrative` (scope `runs:read`) and
`AgentRuntime.narrative(run_id, tenant_id)` return the run in order from its stored state and events:
context built, model calls with the tools they requested, tool calls with argument keys and result
sizes, approvals, policies that acted, verification, memory and the outcome, with totals for tokens,
cost and duration and the run's `trace_id`. It carries no prompt, answer or tool content and does not
depend on what a tracing backend received, so it is the first thing to hand an analyst or an agent.

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

## MCP OAuth connections

OAuth MCP servers (docs/mcp.md, "OAuth for remote MCP servers") add this telemetry. None of it
carries a token, a refresh token, a PKCE verifier, an authorization code or a state.

| Kind | Name | Data |
| --- | --- | --- |
| Run event | `mcp.auth_required` | `code: mcp_not_connected`, `server`, `token_scope`, `start_path`. Emitted when a tool call finds no connection; the call's `tool.completed` has status `failed`. |
| Audit | `tool.call`, outcome `not_connected` | `step_id`, `mcp_server`. |
| Audit | `mcp.oauth.connect`, outcome `completed` | `mcp_server`, `token_scope`; actor is the user who completed the flow. |
| Audit | `mcp.oauth.revoke`, outcome `completed` | `mcp_server`, `removed`, `provider_revoked`. |

Run more than one API worker only with `storage.mcp_oauth_token_store: postgres`: the in-memory
store cannot see a flow started on another worker. Back up `mcp_oauth_encryption_key` with the
database; without it stored connections cannot be read and users must connect again. Refreshes
are serialized within a process. Across workers, a refresh the provider rejects first re-reads
the store and keeps another worker's newer token; only when none has been stored yet can a race
on a rotating refresh token disconnect the user, who then connects again.

## API Admission Rate Limiting and Concurrency Quotas

To prevent run, approval, artifact, and conversation floods, the runtime supports tenant-aware admission rate limiting and in-flight concurrency quotas:

```yaml
api:
  rate_limiting:
    enabled: true
    default_rate_per_minute: 120
    default_burst: 30
    max_concurrent_run_requests_per_tenant: 10
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
   - Separately bounds in-flight request concurrency for run creation (`POST /v1/runs`), tracking in-flight HTTP requests and releasing capacity upon request completion (HTTP 202 response), failure, or client disconnection. This limits concurrent run-creation requests, not the lifetime of background executing runs.

2. **Route Classification:**
   - `run_create`: Run creation endpoint (`POST /v1/runs`). Subject to rate limits and `max_concurrent_run_requests_per_tenant`.
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

## Request Payload Wire-Size Limits

To prevent memory exhaustion and denial-of-service via oversized request bodies, the runtime enforces wire-size limits via pure ASGI middleware (`RequestBodyLimitMiddleware`):

- **General endpoints:** Bounded by `security.max_request_bytes` (default 1 MiB / 1,048,576 bytes).
- **Artifact uploads (`POST /v1/artifacts`):** Bounded by `security.max_artifact_bytes` (default 10 MiB / 10,485,760 bytes).

Semantics:
1. **Header validation:** If `Content-Length` is present, it must be a valid non-negative integer; invalid values return HTTP 400 with code `INVALID_CONTENT_LENGTH`.
2. **Pre-read rejection:** If `Content-Length` exceeds the route limit, the request is immediately rejected with HTTP 413 before reading or buffering any body chunks.
3. **Stream counting:** Regardless of headers (including chunked requests with omitted or inaccurate `Content-Length`), incoming wire bytes are counted as chunks arrive. If accumulated bytes exceed the limit, execution aborts immediately with HTTP 413.
4. **Stable error response:**
   ```json
   {
     "detail": "request payload too large",
     "code": "REQUEST_PAYLOAD_TOO_LARGE",
     "limit_bytes": 1048576
   }
   ```
5. **Decompression:** Wire limits apply to raw incoming bytes. If decompression middleware is introduced in the future, it must enforce separate post-decompression size limits.

## Distributed Worker Operations and Storage Validation

Distributed execution allows scaling background analytical graphs and long-running agent tasks across worker pools:

1. **Worker Fencing & Lease Management:**
   - Background tasks are claimed using exclusive time-bounded leases (`lease_seconds`, default 30s).
   - `DistributedWorker` periodically renews leases during task processing.
   - If a lease expires due to process freeze or network partition, worker fencing ensures subsequent actions fail closed with `ConflictError`, preventing split-brain state mutations.

2. **Retry Policies & Dead-Letter Handling:**
   - Failed tasks undergo bounded retries up to `maximum_attempts` (default 3).
   - If maximum attempts are exhausted, tasks transition to a terminal failed/dead-letter state without further automated execution.
   - Unhandled cancellations immediately release or revoke task claims to ensure prompt rebalancing.

3. **Storage Integration Test Verification:**
   - Local and CI test suites validate PostgreSQL 16 and Redis 7 persistence:
     ```bash
     # Run storage integration tests locally with Docker service containers
     docker run -d --name test-postgres -e POSTGRES_PASSWORD=secret -e POSTGRES_DB=algen_test -p 5432:5432 postgres:16
     docker run -d --name test-redis -p 6379:6379 redis:7

     POSTGRES_DSN="postgresql://postgres:secret@localhost:5432/algen_test" \
     REDIS_URL="redis://localhost:6379/0" \
     pytest -q -p no:cacheprovider -m "integration or postgres or redis"
     ```
   - Automated CI runs verify schema initialization (`001_initial.sql`), optimistic locking on run checkpoints, artifact lifecycle, tool ledger idempotency, and workflow checkpoint persistence.

Back up each durable store enabled by the application according to its retention policy. User deletion must remove tenant/session memory and authorized artifacts while preserving legally required, redacted audit records.

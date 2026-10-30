# Operations guide

Owner: AlgenAI Maintainers  
Status: Active (0.1.x)  
Last reviewed: 2026-10-29  

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
  # postgres_schema: agent_runtime  # optional; see "Sharing a database" below
  initialize_schema: true
  postgres_min_pool_size: 1
  postgres_max_pool_size: 10
  postgres_connect_timeout_seconds: 10   # see "Database outages" below
  postgres_command_timeout_seconds: 60
  memory_retention_seconds: 86400
  # quotas:                     # optional per-tenant storage limits; see "Storage quotas"
  #   default: {artifact_bytes: 10737418240, memory_items: 100000}
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
  # conversation_retention_days: 180
  # workflow_checkpoint_retention_days: 90
  # analytical_graph_retention_days: 30
  # work_item_retention_days: 30
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

recovers non-paused incomplete runs. Completed tool calls return their persisted result; an
interrupted side-effecting call becomes `indeterminate` and requires reconciliation rather than
automatic replay (see [Resolving indeterminate tool calls](#resolving-indeterminate-tool-calls)).
Approval and clarification waits remain paused across restarts until a maintenance sweep expires
them (see [Maintenance and data retention](#maintenance-and-data-retention)); reconciliation waits
remain paused until an operator settles the call.

### Sharing a database: `postgres_schema`

By default the Runtime creates its tables (`algen_agent_runtime_*`) wherever the server's
`search_path` puts them, normally `public`. To keep them apart from an application's own tables in
the same database, set `storage.postgres_schema`:

```yaml
storage:
  postgres_dsn: env://ALGEN_AGENT_RUNTIME_POSTGRES_DSN
  postgres_schema: agent_runtime
```

- Every connection of the Runtime's pool starts with `search_path` set to that schema alone (an
  asyncpg startup parameter), so the migrations, the migration history table and every PostgreSQL
  store (runs, events, audits, approvals, memory, learned memory, artifacts and S3 artifact
  metadata, tool executions, conversations and conversation feedback, workflow checkpoints,
  analytical graphs, the work queue, MCP OAuth tokens and token revocations) use it. A table missing from the schema is never looked up in `public`. Two Runtimes, or a Runtime and
  its tests, can share one database with different schemas.
- With `initialize_schema: true` the schema is created when missing (`CREATE SCHEMA`, which needs
  the `CREATE` privilege on the database). With `false` it must exist, and start-up fails if it does
  not. The database role needs `USAGE` and `CREATE` on the schema to migrate.
- The name must be 1 to 63 lowercase letters, digits or underscores, start with a letter or
  underscore, and not start with `pg_` or be `information_schema`; it is always quoted as an
  identifier. Settings validation refuses anything else. `postgres_schema: public` is allowed, but
  it is stricter than leaving the setting unset: only `public` is on the `search_path`.
- At start-up the Runtime checks `current_schema()` and refuses to start if the schema is not in
  effect. A connection pooler in transaction mode may drop the `search_path` startup parameter:
  with PgBouncer 1.20 or later add `search_path` to `track_extra_parameters`, or use session pooling.
  The same check fails when the schema exists but the role lacks `USAGE` on it.
- Schema creation and migrations run under a PostgreSQL advisory lock, so several replicas
  starting at once against a fresh schema apply them one at a time.
- Unset (the default), nothing changes: no `search_path` is sent and existing deployments keep their
  tables. Moving an existing deployment into a schema is a database operation
  (`ALTER TABLE ... SET SCHEMA` for each `algen_agent_runtime_*` table, including
  `algen_agent_runtime_schema_migrations`) done while the Runtime is stopped; the Runtime does not
  move tables itself. If the setting is turned on while the tables are still in `public`, the
  Runtime starts with an empty schema, and existing runs, pending approvals and tool-execution
  records are not visible to it; start-up logs the warning
  `postgres_schema_is_empty_but_public_has_runtime_tables` naming both schemas.
- The pgvector retriever opens its own connection and keeps its table in `retrieval.<name>.table_name`;
  this setting does not apply to it.

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
| `model_providers` | `any` | `any`: at least one registered provider must be healthy (`failing: ["provider.*"]` otherwise); `all`: every provider is required (the behavior before 0.1.0a16); `none`: providers are only reported. With no providers registered, the rule is met. |
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

## Persistence: restarts, outages, backup and rollback

This section is the single-node contract: one Runtime process per database, with PostgreSQL (and
optionally Redis). The drills in `tests/drills` check every statement here against live
PostgreSQL 17 and Redis 7 containers on each change (see [Running the drills](#running-the-drills)).

### Restart recovery

A process that is killed (SIGKILL, OOM, node loss) leaves only what it had committed. The next
start recovers every unfinished run that is not waiting:

- A run interrupted in a model call calls the model again from its last checkpoint. The step it
  was in is counted once, so repeated crashes do not use up `max_steps`.
- A tool call that completed is replayed from the ledger, never run again. A call interrupted
  while running (ledger `started`) is put to the tool's `reconcile` hook on replay; without one, a
  side-effecting call waits for an operator ([Resolving indeterminate tool
  calls](#resolving-indeterminate-tool-calls)). Keyed tools therefore take effect once, however
  often the process dies: the drills kill five processes in a row in the middle of runs and check
  one ledger record and one external effect per run.
- A run paused for approval or clarification stays paused and is resumed by whichever process
  receives the decision; its approval request, ledger and events are in the database.
- A conversation turn paused for approval resumes on the new process. A turn that was streaming
  when the process died is ended as `failed` with `error_type: InterruptedResponse` (retryable);
  its run is recovered by the runtime and does not repeat its tool effects.
- A workflow run started through the HTTP API is recovered as in [Workflow
  recovery](#workflow-recovery). An agent node with `recovery_policy: retry` starts a new agent
  run; the interrupted agent run is recovered too and completes from its own ledger, but the
  retried attempt makes its own tool calls. A retried node is at-least-once: declare `retry` only
  for nodes whose tools are idempotent or reconcilable.

Recovery is not coordinated between processes: a process that starts while another serves the same
database also drives the other's unfinished runs. The second drive's first checkpoint write fails
its version check (`RunConflictError`) and it stands down without touching the run, and ledger
reservations keep a tool call to one execution, but the model call may be made twice. Run one
process per database (or schema), and do not overlap old and new processes during a deploy for
longer than the drain.

### Database outages

Store calls are bounded, so an unreachable or unresponsive store fails fast instead of hanging a
request, a run or a readiness probe:

| Setting | Default | Bound |
| --- | --- | --- |
| `storage.postgres_connect_timeout_seconds` | `10` | Opening a PostgreSQL connection. |
| `storage.postgres_command_timeout_seconds` | `60` | One PostgreSQL store command; `null` waits indefinitely. Migrations are not held to it. |
| `storage.redis_socket_timeout_seconds` | `10` | Connecting to Redis and one Redis command; `null` waits indefinitely. |

While PostgreSQL (or a Redis run store) is down:

- `/health/ready` returns 503 with the store's dependency `down` (`store.run`, `store.event`, ...)
  and listed in `failing`, within `runtime.readiness.probe_timeout_seconds`. A Redis cache
  (`cache.redis`) is reported down but is optional: cached calls fall through to the tool after
  the socket timeout.
- A store call fails with `StoreUnavailableError` (`kind: unavailable`, `retryable`), and the API
  answers **503** with `Retry-After`, never a driver exception. Starting a run, deciding an
  approval or answering a turn during the outage is refused this way; retry it once the store is
  back.
- A run in flight keeps its last checkpoint. It is not failed: the process drives it again from
  that checkpoint once the store answers (retrying with backoff from 0.5 to 30 seconds), with the
  same caller credentials, and `runtime.wait` follows it. Completed tool calls are replayed from
  the ledger. If the process stops first, the next start recovers it.
- When the store returns, the connection pool reconnects without a restart. A lost PostgreSQL
  server leaves every pooled connection dead; the first call that meets one fails with
  `StoreUnavailableError` and the pool replaces the rest, so one request fails rather than one per
  pooled connection.
- A process that starts while PostgreSQL is down fails its start with `StoreUnavailableError`
  after the connect timeout; the orchestrator restarts it.

A write interrupted by the outage may or may not have been applied: a run's checkpoint that was
written is found by its version check, and a tool call that may have run is `started` in the ledger
and is reconciled. Event and audit writes interrupted by an outage can be lost with the request
that made them.

Run stores and the stores that reference runs belong together: `event_store`, `approval_store` and
`tool_execution_store` `postgres` require `run_store: postgres`, since their rows reference the run
in PostgreSQL. Configuration validation rejects other combinations.

### Failover

A single node sees a primary failover as a restart of its database: every connection drops, and the
server comes back (at the same address) with what it had committed. The drills restart the
PostgreSQL container under load and check that nothing committed is lost, the pool reconnects, the
run in flight completes once and a paused run keeps its pending approval.

Replica promotion and its guarantees are the database operator's concern. What the Runtime needs:

- A stable endpoint in `postgres_dsn` (a DNS name, virtual IP or proxy that follows the primary);
  the pool reconnects through it on the next call.
- Synchronous replication (or a managed service with zero data loss on failover) if no committed
  checkpoint may be lost. With asynchronous replication, the last transactions before a failover
  can be lost: a run's checkpoint can fall behind its ledger (the call replays from the ledger), a
  tool call can be missing from the ledger although it took effect (reconcile with the external
  system), and an approval decided just before can be pending again.
- A pooler in front of PostgreSQL in session mode, or one that keeps startup parameters when
  `postgres_schema` is set (see [Sharing a database](#sharing-a-database-postgres_schema)).

### Backup and restore

Back up the Runtime's database (or its `postgres_schema`) as a whole: runs, events, audits,
approvals, artifacts metadata, the tool-execution ledger, conversations, workflow checkpoints,
memory, analytical graphs, work items, MCP OAuth tokens, token revocations and the
`algen_agent_runtime_schema_migrations` table. They reference each other (a ledger record or an
approval refers to its run), so back them up in one snapshot, never table by table. S3 artifact
blobs and a Redis run store need their own backups; a Redis run store has no ledger or approvals of
its own, so prefer PostgreSQL for anything that must be restored.

A logical backup with `pg_dump` reads one consistent snapshot and can run while the Runtime serves:

```bash
pg_dump --format=custom --file=runtime.dump "$DATABASE_URL"
```

For point-in-time recovery use the database's base backups and WAL archiving (or the managed
service's equivalent) instead; the same restore steps apply.

To restore:

1. Stop the Runtime (or point it elsewhere): two processes must not drive the same runs.
2. Restore into an empty database:
   `createdb runtime_restored && pg_restore --no-owner --exit-on-error -d runtime_restored runtime.dump`.
3. Start the Runtime with `postgres_dsn` pointing at the restored database. Startup applies any
   newer migrations, recovers unfinished runs and serves; paused runs keep their approvals, ledger
   and events and resume on a decision.
4. Reconcile what happened after the backup was taken. The external effects of tool calls made
   after the snapshot are not in the restored ledger, so a recovered run may make them again unless
   the tool's own idempotency key or `reconcile` hook prevents it; approvals decided after the
   snapshot are pending again; runs started after it do not exist. List the restored
   `indeterminate` and `started` calls with `GET /v1/tool-executions` and settle them first.

The drill `tests/drills/test_backup_restore.py` takes a dump of a live database with a paused run,
restores it into a new database and resumes the run there.

### Upgrades and rollback

Migrations are forward-only and additive: each adds tables, nullable columns, indexes or widened
constraints, never drops or rewrites what an earlier release reads. Stored JSON (run state, tool
results, approval requests) is written so the previous release can read it, so:

- **Upgrade:** start the new release. It applies the missing migrations under an advisory lock
  (concurrent starts wait for one another) and carries on what the previous release left; the drill
  starts `0.1.0a17` on an empty database, pauses a run, then starts the current code, which applies
  `015` and resumes the run.
- **Rollback:** stop the new release and start the previous one on the same database. Leave the
  newer migrations in place: the previous release ignores migration rows it does not know and does
  not read the new columns or tables. The drill rolls back to `0.1.0a17` after the current code has
  migrated and written, and reads, lists and resumes the current code's runs there.

| Migration | Since | Change | Safe to leave on rollback |
| --- | --- | --- | --- |
| `015_approval_agent.sql` | unreleased (after `0.1.0a17`) | Nullable `agent` column and an index on approvals. | Yes; `0.1.0a17` does not read it. Approvals created by the previous release have no `agent`. |

Restore from backup instead of rolling back only when a release's notes say a migration is not
backward compatible; none is so far. Take a backup before every upgrade.

### Running the drills

`scripts/integration-tests.sh` starts throwaway PostgreSQL and Redis containers and exports their
names (`ALGEN_TEST_CONTAINER_ENGINE`, `ALGEN_TEST_POSTGRES_CONTAINER`,
`ALGEN_TEST_REDIS_CONTAINER`) so the outage, failover and backup drills can pause, restart and exec
into them:

```bash
scripts/integration-tests.sh tests/drills -m drill                     # every drill but the soak
ALGEN_DRILL_SOAK_RUNS=200 scripts/integration-tests.sh tests/drills -m soak -s
```

Drills that need a container or the previous release's git tag skip without them. The soak drill
mixes tool-calling runs, approvals and conversation turns at `ALGEN_DRILL_SOAK_CONCURRENCY`
(default 20) and checks that every run ends once, the pool stays within its maximum and is
released, no task is left behind and traced memory growth stays under
`ALGEN_DRILL_SOAK_MAX_GROWTH_MB` (default 64). It lifts the mock provider's request rate limit
(600 a minute by default), which would otherwise pace it. Local runs (Apple Silicon laptop,
PostgreSQL 17 and Redis 7 in podman, pool of 10, concurrency 20; a tool-calling run, an approval
round trip or a conversation turn per item):

| Items | Seconds | Items/s | p50 | p95 | Max | Pool peak | Traced growth |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 200 | 10.2 | 19.6 | 0.99 s | 1.30 s | 1.42 s | 10 of 10 | 0.34 MB |
| 1000 | 53.7 | 18.6 | 1.05 s | 1.39 s | 1.63 s | 10 of 10 | 1.17 MB |

Every item ended completed with its effect once, the pool waited at its maximum without errors and
was released, and no task was left. The item latency is mostly the drill's own polling.

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
            runtime-approvers: [approvals:read, runs:write]
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
| Wrap-up notice | `tool.wrap_up` (`tool_calls_left`, `reason: steps\|tokens\|model_calls`) | unchanged | `run.wrap_up`, `notified` | `agent.wrap_up.tool_calls_left` on the `agent.step` span (`agent.run` for a parallel reply) |

`tool.refused` also carries `refusal_count` (refusals of that tool so far in the run) and
`refusal_limit_reached` (the note told the model to stop); `tool.repeated` for a call limit
carries `call_limit`. The same values are on the span as `tool.guard.reason_code`,
`tool.guard.refusal_count`, `tool.guard.refusal_limit_reached` and `tool.guard.call_limit`, and in
the audit metadata beside `step_id`. None of them carries argument values. The run narrative lists
the three events. Alert on runs whose `refusal_limit_reached` is true or with many
`tool.repeated` events: the model is stuck on a tool's contract or its instructions.

A held call has the same step events as one that runs: `step.started`, then `tool.started` with
the guard's status (`refused`, `repeated` or `over_limit`) instead of `running`, then the guard's
event and `tool.completed` with that status and its `reason_code`. A consumer that tracks calls
from `tool.started` sees every call. A reply's calls are announced in the order the model sent
them: in a parallel reply every call's `step.started`, then every call's `tool.started`, before
any of them runs, and their results and `tool.completed` events follow in the same order.

## Policy boundaries, fail modes and signed bundles

### Boundaries and fail modes

The Runtime evaluates its policy engine at eleven boundaries. When a policy raises, returns
something other than a `PolicyDecision`, or runs past `policies.evaluation_timeout_seconds`, the
boundary's fail mode decides what happens:

- `closed` refuses the value, exactly as a `deny` decision would (`reason_code: policy.error`).
- `open` skips that policy and lets the boundary's other policies decide; it never skips them.
  A redaction policy that still runs still redacts.

A policy that raises `PolicyDeniedError` has decided; that is a denial, not a policy error, in
either mode.

| Boundary | Evaluated on | A refusal (deny or `closed` error) | Default |
| --- | --- | --- | --- |
| `input` | the run's input; each steering message before it is delivered | the run fails | `closed` |
| `before_retrieval` | the request text before context is built | the run fails | `closed` |
| `after_retrieval` | loaded conversation history; the built context messages | the run fails | `closed` |
| `before_model` | every model request, compaction included | the run fails | `closed` |
| `after_model` | every model reply's text, a compaction summary | the run fails | `closed` |
| `plan_tool` | each requested tool call, before guards and approval | the call is refused (`tool_policy.on_error`) | `closed` |
| `before_tool` | the call's arguments in the tool executor (agent runs and workflow tool steps); a redaction here also refuses the call | the call is refused | `closed` |
| `after_tool` | the tool's output, before it is recorded, cached or shown to the model | the call fails | `closed` |
| `final_response` | the verified answer | the run fails | `closed` |
| `before_memory_write` | conversation memory, learned-memory proposals, working state, loaded skills, compaction archives | the run (or the memory tool call) fails | `closed` |
| `after_memory_write` | the count of messages just written | the run fails (the memory is already written) | `closed` |

Every default is `closed`, which is what each boundary already did before fail modes were
configurable: an exception from a policy failed the run or the call. What changed is the shape of
that failure (a `policy.error` denial instead of the policy's own exception) and that it is now
recorded. No boundary is `open` unless you configure it.

```yaml
policies:
  on_error: closed                 # closed | open, for every boundary
  boundary_on_error:               # per-boundary overrides
    after_memory_write: open
  evaluation_timeout_seconds: 5    # per policy; unset (no limit) by default
```

Each handled policy error leaves:

- an audit record `action: policy.error`, `outcome: fail_closed | fail_open`, with the boundary,
  policy, error type, fail mode, `timed_out` and, at tool boundaries, the tool name, in the run's
  tenant;
- the counter `algen_agent_runtime.policy.errors` with `boundary`, `policy`, `fail_mode` and
  `error_type`;
- a `policy_error` warning log line with the same fields. The error's message is never recorded:
  it may quote the payload.

At the run's own boundaries the `policy.evaluated` event also carries `fail_mode` and
`error_count`. Alert on any `fail_open` record: a boundary let a value through without one of its
policies.

Fail modes apply to the built-in `CompositePolicyEngine`, which the container builds. An engine
you pass to `AgentRuntime` yourself keeps its own error behaviour. Other checks are deterministic
and always refuse: tool-call guards, approvals, tool-result injection screening, query governance,
learned-memory and context-artifact scope checks. Two settings outside the engine have their own
fail modes: `telemetry.traccia.governance_fail_open` (default `false`) and
`api.rate_limiting.fail_closed` (default `true`).

### Signed policy bundles

A bundle distributes policy rules as one YAML or JSON file. Its metadata is part of the signed
bytes:

```yaml
format: 1
name: acme-baseline          # lowercase letters, digits, ., _ and -
version: 7                   # an integer; raise it for every release
issued_at: 2026-10-29T00:00:00Z
expires_at: 2027-01-31T00:00:00Z   # optional
description: Company-wide agent rules
rules:
  deny_tools: ["payments.*", "admin.*"]        # refused at plan_tool and before_tool
  require_approval_tools: ["crm.export"]       # a person approves first, whatever the side effect
  deny_patterns:
    - name: project-codename
      pattern: "(?i)project\\s+nightingale"
      boundaries: [input, after_model, final_response]
  redact_patterns:
    - name: employee-id
      pattern: "EMP-\\d{6}"
      replacement: "[EMPLOYEE_ID]"
      boundaries: [after_tool, after_retrieval, final_response]
```

Tool names are `fnmatch` globs. A pattern rule applies to every boundary except
`after_memory_write` unless it lists its own. Each bundle becomes the policy `bundle:<name>`, which
runs for every agent whatever its `guardrail_policy` (like authorization), so an agent definition
cannot opt out of it. A deny from any policy wins over another policy's approval requirement at the
same boundary.

Sign and verify with the CLI. Signing needs the `encryption` extra (`cryptography`); an unsigned
bundle loads without it.

```bash
algen-agent-runtime policy keygen --private-key signing.pem --public-key signing.pub
algen-agent-runtime policy sign acme-baseline.yaml --key env://POLICY_SIGNING_KEY   # or file://signing.pem
algen-agent-runtime policy verify acme-baseline.yaml --trusted-key file://signing.pub --min-version 7
```

`keygen` writes the private key readable by its owner only and refuses to overwrite a file. Keep it
in your secret manager and sign in your release pipeline, not on Runtime hosts. `sign` writes
`<bundle>.sig` (base64 Ed25519 over the file's exact bytes). `verify` checks a bundle exactly as
start-up does and prints its name, version, times, SHA-256 and the signer's key id.

```yaml
policies:
  require_signed: true
  trusted_keys:                      # Ed25519 public keys: PEM or base64 raw 32 bytes
    - env://POLICY_PUBLIC_KEY
    - file:///etc/algen/policy-signing-2026.pub
  bundles:
    - path: /etc/algen/policies/acme-baseline.yaml
      # signature: /etc/algen/policies/acme-baseline.yaml.sig   (the default)
      min_version: 7                 # refuse an older signed bundle put back in place
      # trusted_keys: [...]          # this bundle only; policies.trusted_keys otherwise
```

Start-up refuses (and the container does not build) when:

- `require_signed` is true and a bundle has no signature file;
- a signature file exists but does not verify against any trusted key, whatever `require_signed`
  says, or a bundle is signed and no trusted keys are configured;
- the bundle has expired, is issued more than five minutes in the future, or its version is below
  `min_version`;
- the file is not a valid bundle, or two bundles have the same name.

Key references must be `env://` or `file://`; a literal key in configuration is refused. They are
resolved before `cryptography` is imported, so a missing variable is reported as such. Each loaded
bundle logs `policy_bundle_loaded` with its name, version, SHA-256, whether it was signed and the
key id. With `require_signed: false` (the default) an unsigned bundle loads with a warning, for
development.

To rotate a key, add the new public key to `trusted_keys`, sign new bundles with it, deploy, then
remove the old key. To roll a bundle forward, raise its `version`, sign it, and raise
`min_version` with the deployment that ships it.
## Run limits

Beside steps, tokens, cost and latency, every agent's `budget` bounds what one run can make the
model produce and how many events it can record. The defaults are far above what a normal run
uses, so existing agents are unaffected; lower them for agents that face untrusted input.

| Setting | Default | Bounds | When reached |
| --- | --- | --- | --- |
| `budget.max_model_calls` | 1,000 | Model calls in the run: steps, rejected completions that were retried, history compaction and verification repairs. | Like the step limit: a continuation (`continuation_policy`) adds `step_increment` calls, otherwise the run fails. The wrap-up notice counts the calls left (`reason: model_calls`). |
| `budget.max_response_chars` | 1,000,000 (at least 1,000) | One model response: its text plus its tool calls' arguments as compact JSON. | The run fails at once. A streamed response is cut off as soon as it passes the limit; a complete one is charged to the run's usage first. |
| `budget.max_events` | 100,000 (at least 100) | Run events recorded for the run. | Further events are dropped, except those that end or pause the run (`run.completed`, `run.failed`, `run.cancelled`, `approval.required`, `clarification.required`, `tool.reconciliation_required`, `mcp.auth_required`, `budget.refused`, `steer.undelivered`), and the run fails at its next step. |
| `runtime.stream_delta_interval_ms` | 50 (0 to 5,000) | How often streamed text is recorded. | Chunks that arrive within the interval are joined into one `model.delta` event (the first is sent at once, the rest at the end of the stream at the latest), so a long stream does not use up `max_events`. `0` records every chunk, as `AgentRuntime` does when constructed directly. |

A run that ends at a limit fails with `RunState.error` naming it and a `run.failed` event with
`reason: limit_exceeded` and `limit` (`model_calls`, `response_chars`, `events`, or the existing
`execution_steps`, `model_tokens` and `estimated_cost`), and the audit log gets a
`run.limit_exceeded` record (outcome `failed`) with the limit, steps, model calls and tokens used;
the `agent.run` span carries `agent.run.limit_exceeded`. The event count is the run's event
sequence, so it holds across pauses and restarts. Alert on `run.limit_exceeded`: a run that keeps
reaching a limit is looping or being fed hostile input.

## Storage quotas

`storage.quotas` limits what each tenant can keep in the artifact store, session memory and learned
memory. Every limit is unlimited by default; `default` applies to every tenant and a `tenants` entry
overrides only the fields it sets (`null` lifts a limit for that tenant):

```yaml
storage:
  quotas:
    default:
      artifact_count: 10000
      artifact_bytes: 10737418240    # 10 GiB
      memory_items: 100000           # session memory messages
      memory_bytes: 1073741824
      learned_memory_items: 5000     # learned-memory records that hold content
      learned_memory_bytes: 52428800
    tenants:
      enterprise-tenant: {artifact_bytes: 107374182400}
      internal-tools: {memory_items: null}
```

Bytes are what is stored: artifact payload bytes (S3 artifacts included) and the JSON size of each
memory message or learned-memory record. Expired records count until the maintenance sweep purges
them. A write that would take a tenant past a limit writes nothing and raises `QuotaExceededError`;
the API answers **413** with `code: QUOTA_EXCEEDED`, `resource`, `dimension` (`items` or `bytes`),
`limit` and `used` (the usage the write would have reached). 413 rather than 429 because retrying
cannot succeed until the tenant deletes data or its quota is raised. Deletes, and writes that do not
grow usage, are always allowed, so a tenant over a lowered quota can clean up. Inside a run, a full
artifact quota fails the tool-result offload as a tool error (`could not store context artifact:
storage quota exceeded ...`), a full learned-memory quota fails the memory tool call, and a full
session-memory quota keeps the run's answer but not the turn's memory: `memory.written` reports
`operation: skipped, reason: quota_exceeded` and a `storage.quota_exceeded` audit record (outcome
`refused`) names the resource.

`GET /v1/storage/usage` (scope `usage:read`) returns the caller's tenant usage and limits per
resource; `container.storage_usage.usage(tenant_id)` does the same in Python.

| Backend | How usage is kept |
| --- | --- |
| PostgreSQL (and S3 metadata) | Migration `016_resource_bounds.sql` adds `algen_agent_runtime_tenant_usage` (tenant, resource, items, bytes) and triggers on the artifact, memory and learned-memory tables that update it in the same transaction as every insert, update and delete, from any code path, the maintenance sweep and manual SQL included. The migration backfills it from existing rows. A quota-checked write locks the tenant's usage row (`SELECT ... FOR UPDATE`), writes, re-reads the row and rolls back when the limit is passed, so concurrent writes for one tenant are serialized and cannot overshoot. Tenants without a limit write as before; the triggers still keep their usage. S3 checks once before uploading and again under the lock when it records the artifact, deleting the uploaded object if the second check fails. |
| In memory | Computed from the stored records under the store's lock. |
| Redis (session memory) | Lua scripts keep each tenant's usage beside its sessions and forget sessions whose list has expired. Enabled only when some quota is configured; sessions written before that are not counted until they expire. The scripts touch several keys, so this needs a Redis that is not clustered. |

On PostgreSQL the triggers update one usage row per tenant and resource, so concurrent writes of
one tenant wait briefly for each other on that row (each holds it until its own statement or
transaction commits); a quota-checked write holds it for the whole write. Writes of different
tenants never wait for each other. Run the migration in a quiet period: creating the triggers locks
the artifact, memory and learned-memory tables against writes until the backfill commits.

## Resolving indeterminate tool calls

Every side-effecting tool call is reserved in the tool-execution ledger under its idempotency key
(`<run_id>:<tool_call_id>`) before it runs. When a call fails without a clear outcome (a timeout or
a dropped connection after the request left), the ledger records it `indeterminate`. When the
process stops mid-call, the reservation stays `started`. Neither is ever retried automatically:
someone has to find out whether the effect happened.

### What happens to the run

By default (`tool_policy.on_indeterminate: on_error`) an indeterminate call is handled like any
failed call: the run fails, or with `on_error: return_to_model` the model hears that it failed.
Settling the call later is record-keeping only; an ended run is never reopened, because its
caller, conversation or parent workflow has already acted on the ending.

With `on_indeterminate: pause`, the run pauses in `awaiting_reconciliation` instead, with a
`tool.reconciliation_required` event and a `pause_payload` of `kind: tool_reconciliation`
naming the tool, call and idempotency key (never the arguments). Settling the call continues the
run: `completed` gives the model the recorded result (screened like any tool result), or a note
that the call took effect when no result was recorded; `failed` gives it a `ToolCallNotApplied`
tool error; a successful compensation gives it `ToolCallCompensated`. A paused run keeps its
remaining deadline and is not expired by the maintenance sweep; cancel it to give up. In a
parallel batch, the other calls are recorded first and the run pauses on the first unknown one.
Read-only tools never pause a run. A resumed run has no caller credentials, as after recovery.

```yaml
agents:
  - name: crm-assistant
    tool_policy:
      on_indeterminate: pause
```

### Runbook

1. **Find the calls.** `GET /v1/tool-executions` lists the caller's tenant's `indeterminate` and
   `started` calls, least recently changed first. Filter with `status` (repeatable; also
   `completed`, `failed`, `compensating`, `compensated`, `compensation_failed`), `run_id`, `tool`,
   `min_age_seconds` and `limit`. A `started` call younger than its tool's timeout may still be
   running: use `min_age_seconds`. `GET /v1/tool-executions/{key}` reads one. Responses never carry
   the result value, only `has_result` and `result_reference`, plus the error type and every
   earlier decision (`resolutions`).
2. **Ask the tool, if it can tell.** `POST /v1/tool-executions/{key}/reconcile` with
   `{"reason": "..."}` calls the tool's `reconcile` hook, which looks the idempotency key up in the
   external system. `completed` and `failed` are recorded; `unknown` changes nothing (outcome
   `unknown`). A hook that raises or times out returns 400 and changes nothing. Tools without a hook
   return 409.
3. **Or state the outcome.** `POST /v1/tool-executions/{key}/resolve` with
   `{"status": "completed" | "failed", "reason": "...", "result": {...}, "result_reference": "...",
   "expected_status": "indeterminate"}`. `reason` is required. `result` is optional, only for
   `completed`, and must fit the tool's output schema and size limit. `expected_status` makes the
   request fail with 409 if someone changed the call since you looked.
4. **Undo it, if it should not stand.** `POST /v1/tool-executions/{key}/compensate` with
   `{"reason": "..."}` runs the tool's `compensation` for a `completed`, `indeterminate` or
   `compensation_failed` call (see [Compensation](#compensation)).
5. **Continue a stuck run.** If the Runtime stopped between settling a call and continuing its
   run, `POST /v1/tool-executions/{key}/resume` continues a run still `awaiting_reconciliation`
   on an already-settled call.

`resolve`, `reconcile` and `compensate` take `"resume": false` to settle the call without
continuing a waiting run. All responses are `{"outcome", "run_resumed", "execution"}`.

Python: `runtime.reconciliation` (`ToolReconciliation`) has `list`, `get`, `resolve`,
`auto_reconcile`, `compensate` and `resume` with the same rules; it returns full records,
including results. `ToolReconciliation.stale_started_after` (default 10 minutes) is how long a
`started` call must be unchanged before it may be resolved or auto-reconciled.

### Rules

- **Scope.** Every route needs `tools:reconcile` and acts only within the caller's tenant; another
  tenant's key is 404. The caller's user id is recorded as the actor. Grant the scope only to
  operators.
- **Compare-and-set.** Each decision changes the ledger only if the status is still the one read,
  so of two concurrent decisions exactly one wins and the other gets 409. A call that finishes
  after an operator settled it cannot overwrite that decision: `complete` and `fail` only change a
  `started` record, and the late outcome is logged as `tool_execution_late_outcome_ignored`.
- **Record.** Each decision appends `{action, previous_status, status, actor_id, reason,
  result_reference, at}` to the record's `resolutions`.
- **Re-execution.** A call settled `failed` counts as not having happened: if a still-running run
  replays it, it runs again, as any failed call does.

### Compensation

Compensation runs only when an operator asks for it; the Runtime never compensates by itself,
not on run failure, cancellation or recovery. Automatic compensation is not offered: the Runtime
cannot tell whether a run's failure makes its earlier effects wrong, a failure seen during
recovery may be transient, and undoing an indeterminate call can itself cause a side effect when
the original never happened. An operator, who can check the external system, decides.

The ledger moves to `compensating` (compare-and-set) before the handler runs, so one request runs
it at most once and concurrent requests get 409. Success records `compensated`; an exception
records `compensation_failed` with the error type, and only that status may be compensated again.
If the process stops while the handler runs, the call stays `compensating` and is never
compensated again by itself: check the external system and settle it with `resolve` and
`"status": "compensated"` or `"compensation_failed"`. The handler gets the recorded result, or
for an indeterminate call an empty result whose metadata is
`{"execution_status": "indeterminate"}`, and the call's context without caller credentials.

### Replay-time reconciliation

When a recovered run replays a call that is still `started` or `indeterminate`, and its tool has a
`reconcile` hook (and did not set `reconcile_on_replay=False`), the executor asks the hook first.
`completed` records the result (checked against the output schema and the `after_tool` policies)
and the run continues with it without running the tool again; `failed` records `failed` and the
call fails without running again; `unknown`, an error or a timeout leaves the record as it was,
and the run fails or pauses as configured. The decision is recorded with actor `runtime`.

### Events and audit records

| Kind | Name | Data / metadata |
| --- | --- | --- |
| Run event | `tool.reconciliation_required` | `tool`, `idempotency_key`, `status` (`indeterminate` or `started`) |
| Run event | `tool.reconciled` | `tool`, `idempotency_key`, `status`, `previous_status`, `method` (`operator`, `tool` or `replay`) |
| Run event | `tool.compensation.started`, `tool.compensation.completed`, `tool.compensation.failed` | `tool`, `idempotency_key`, `status`; `error_type` on failure |
| Run event | `tool.completed` | `status` `completed` or `failed`, with `reconciled: true`, when a waiting run continues |
| Audit | `tool.call`, outcome `awaiting_reconciliation` | `step_id`, `idempotency_key` |
| Audit | `tool.reconcile`, outcome `completed` / `failed` / `compensated` / `compensation_failed` / `unknown` / `error` / `resumed` | `run_id`, `step_id`, `idempotency_key`, `method`, `previous_status`, `reason`, `result_reference`, `error_type` |
| Audit | `tool.compensation`, outcome `started` / `completed` / `failed` | `run_id`, `step_id`, `idempotency_key`, `previous_status`, `reason`, `error_type` |

Events carry no arguments, results or reasons; reasons appear only in audit records and the
ledger. Audit records name the operator as `actor_id` (`runtime` for replay-time reconciliation).

### Ledger writes and run checkpoints

The ledger and the run checkpoint are separate writes, even on one PostgreSQL database: the
ledger completion happens inside the tool executor, and the checkpoint that records the answered
call is saved later, after the result is screened, offloaded and announced. The order (ledger
first, then checkpoint) is safe without a shared transaction:

- a crash before the ledger completion leaves the reservation `started`; replay never runs the
  call again, and it is reconciled as above;
- a crash between the ledger completion and the checkpoint leaves the checkpoint with the call
  still pending; recovery replays it, the ledger returns the recorded result (after the tool's
  `replay_validator`), and the tool does not run again. The run state gets exactly one tool
  message for the call. Run events and audit records written before the crash (`tool.completed`,
  `tool.call`) may appear twice: treat them as at-least-once and deduplicate by `step_id`.

`tests/unit/test_tool_reconciliation.py` and `tests/integration/test_tool_reconciliation_postgres.py`
stop the process between the two writes and check this. Migration
`014_tool_execution_reconciliation.sql` adds the new statuses as a `NOT VALID` check constraint
and an index on `(tenant_id, status, updated_at)`.

## Approval inbox

A run that needs a human decision pauses with `status: awaiting_approval` and the approval's id in
`pause_payload.approval_id`. To show everything waiting for a decision, list the tenant's approval
requests instead of polling runs:

| Route | Scope | Returns |
| --- | --- | --- |
| `GET /v1/approvals` | `approvals:read` | The tenant's approval requests, newest first. Query: `status` (`pending`, `approved`, `rejected`, `modified`, `expired`), `run_id`, `agent`, `limit` (1 to 500, default 50), `offset` (default 0). |
| `GET /v1/approvals/{approval_id}` | `approvals:read` | One request; `404` for another tenant's. |

**Scope.** The inbox lists every user's pending actions in the tenant, conversation turns
included, so it needs its own scope, `approvals:read`, rather than `runs:read`, which most agent
clients hold. Grant it to approvers and operators on purpose (for example through `role_scopes`
above). `limit` is 1 to 500 and `offset` 0 to 1,000,000; anything else is a `422`.

Decide as before with `POST /v1/runs/{run_id}/approval` (scope `runs:write`), or for a
conversation turn with `POST /v1/conversations/{id}/turns/{turn_id}/approval`. A run started for a
conversation turn belongs to the conversation's owner, as on the conversation routes: every
`/v1/runs/{run_id}` route (read, narrative, events, cancel, clarification, approval and steer)
answers another user of the tenant `404`, whatever their scopes. Runs started directly with
`POST /v1/runs` are checked against the tenant only, as before.

Each item is the stored `ApprovalRequest`: `id`, `run_id`, `step_id`, `tenant_id`, `agent`,
`proposed_action`, `side_effect_summary`, `redacted_parameters` (redacted before they were stored,
the same values as the run's `pause_payload`), `risk`, `created_at`, `expires_at`, `status` and, after
a `modified` decision, `modified_parameters`. The approver's `modified_parameters` are stored as
sent; the inbox routes return them with the same redaction as `redacted_parameters` (top-level
`password`, `secret`, `token`, `api_key` and `authorization` keys, any case, become `[REDACTED]`).
A pending request past `expires_at` is reported as `expired`: `status=pending` leaves it out and
`status=expired` includes it. Reading never changes the stored request; it moves to `expired` when
someone tries to decide it, as before.

**Storage.** Migration `015_approval_agent.sql` adds an `agent` column to
`algen_agent_runtime_approvals` and an index on `(tenant_id, agent, created_at DESC)`. The
PostgreSQL store keeps `agent` and `created_at` in columns, not in the approval JSON, so the JSON
keeps the 0.1.0a17 shape and an a17 process can still read and resume these requests during a
rolling deploy or after a rollback. `created_at` is the row's creation time (the database clock),
used both to order and to display. Requests stored before this release have no `agent` (an `agent`
filter leaves them out); in the in-memory store they also have no `created_at`.

In Python, `runtime.approvals.list(tenant_id, status=..., run_id=..., agent=..., limit=...,
offset=...)` returns the same list. Results are tenant-scoped by the authenticated principal, like
every run route. Paging is by offset over a list ordered by creation time, so a request created
between two page reads shifts later pages by one; page with a small `limit` and de-duplicate by `id`.
Requests created at the same instant are ordered by insertion in the in-memory store and by `id` in
PostgreSQL; both are stable between calls.

## Steering a running run

A user can add to a run while it works: `runtime.steer(run_id, tenant_id, text)` queues a message
and returns it as a `SteeringMessage` (`id`, `text`, `queued_at`). Over HTTP it is
`POST /v1/runs/{run_id}/steer` with `{"text": "..."}` (scope `runs:write`, tenant-scoped like every
run route), which answers `202` with `{"run_id", "steer_id", "status": "queued"}`. A run started
for a conversation turn can be steered only by the conversation's owner; any other user of the
tenant gets `404`, as on the approval and clarification routes.

- **When the model sees it.** At the run's next step boundary: after the results of every call in
  the model's last reply and before the next model call, as one user message with a text block
  per queued message, in the order they were sent. It is checked at the `input` policy boundary
  like the run's input (context `operation: steering`). A `steer.delivered` event and a
  `run.steer` audit (`delivered`, with `steer_ids`) follow.
- **When the run ends first.** Messages still waiting when the run completes, fails, is cancelled
  or times out (for example, sent during the model's final answer) are never delivered. They are
  `RunResult.undelivered_steering` from `runtime.wait`/`run`, stay in `RunState.pending_steering`,
  and a `steer.undelivered` event announces them, so a host can hand them back to the user or
  start the next turn with them.
- **Paused runs.** A run awaiting approval, clarification or reconciliation keeps its messages in
  its checkpoint (`RunState.pending_steering`) and delivers them after it is resumed, at the next
  boundary. Messages sent to a running run are checkpointed at its next state transition, so a
  restart loses at most those sent since the last one.
- **Events.** `steer.delivered` and `steer.undelivered` carry `count` and `messages`, a list of
  `{id, chars}`; the text is added (`text`) only when `telemetry_include_content` is on. The run
  narrative tells them as `steering` entries without the text. `GET /v1/runs/{run_id}` leaves
  `pending_steering` out, like `messages`. An empty `pending_steering` is also left out of the
  stored checkpoint, so 0.1.0a17 replicas can load it during a rolling deploy.
- **Limits and errors.** At most `MAX_PENDING_STEERING` (16) messages wait per run and each is at
  most `MAX_STEERING_CHARS` (8,000) characters after trimming. Empty or longer text raises
  `ValueError` (HTTP `422`); a full queue, an ended run, or a run executing in another process
  raises `ConflictError` (HTTP `409`). Another tenant's run is `NotFoundError` (HTTP `404`).
- **Not yet:** conversation turns do not expose steering; a host steers the turn's run by its id
  (`pending.run_id`).

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
| Conversations | off | With `conversation_retention_days` set, deletes conversations not updated within that window, together with their messages, conversation events and feedback. A conversation is kept while any of its messages is newer than the window or a turn is in progress or waiting (`accepted`, `streaming`, `awaiting_approval`, `awaiting_input`). Reports `conversations`, `conversation_messages`, `conversation_events` and `conversation_feedback`. |
| Workflow checkpoints | off | With `workflow_checkpoint_retention_days` set, deletes `completed`, `failed` and `cancelled` workflow runs not updated within that window, except a child run whose parent run is unfinished (the parent's recovery reads it). Reports `workflow_checkpoints`. |
| Analytical graphs | off | With `analytical_graph_retention_days` set, deletes `completed`, `failed` and `cancelled` graphs not updated within that window. Reports `analytical_graphs`. |
| Work items | off | With `work_item_retention_days` set, deletes `completed` and `failed` (dead) distributed work items not updated within that window. Their deduplication keys are released: enqueueing the same key later creates a new item. Reports `work_items`. |

Terminal-run deletion is conservative. It keeps runs that still have a `started`, `indeterminate`,
`compensating` or `compensation_failed` tool execution, so operators can reconcile side effects
first, and runs that belong to a workflow
that has not finished. Artifacts follow their own expiry; deleting a run clears their `run_id`. The runs of a deleted
conversation or workflow follow the terminal-run window, and a run whose workflow is unfinished is
kept; deleting a finished workflow checkpoint lets its runs be deleted. The conversation, workflow,
graph and work-item windows apply to PostgreSQL stores and to stores kept in memory.

Work is done in batches (`batch_size`, `max_batches_per_sweep`) with `FOR UPDATE SKIP LOCKED`, so
several replicas or a CronJob can sweep concurrently without blocking request traffic or deleting
the same rows twice. Migrations `010_retention_indexes.sql` and `016_resource_bounds.sql` add the
supporting indexes; on large existing tables, create them `CONCURRENTLY` before upgrading to avoid a
long write lock. Choose
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
externally visible tools always execute normally. A tool whose result depends on the caller's user,
run or step beyond the policy's scope declares it in `cache_context`, which joins its key (see
[Caching results](tool-authoring.md#caching-results)); a call that carries caller credentials is
never cached. Cached provider usage is reported as cached tokens with zero incremental
configured-provider cost.

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
  # Runtime metrics (cache, governance, auth, worker counters...) to an OTLP collector:
  # otlp_metrics_endpoint: https://otel-collector.example.com/v1/metrics
  # otlp_metrics_export_interval_seconds: 60
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

**Metrics.** The Runtime records OpenTelemetry metrics (cache operations and latency,
governance decisions, authentication attempts, analytical nodes, distributed work items, method
and model-service calls). Set `telemetry.otlp_metrics_endpoint` to an OTLP/HTTP metrics endpoint
to export them: the Runtime installs a meter provider with a periodic reader
(`otlp_metrics_export_interval_seconds`, default 60). `otlp_endpoint` alone exports traces only,
and no meter provider is installed, so an application can install its own. Traccia's metrics
(`traccia.enable_metrics`, `traccia.metrics_endpoint`) are configured by the Traccia SDK. A meter
provider installed before the Runtime applies its telemetry settings, by Traccia or by the
application, is kept, and `otlp_metrics_endpoint` is then ignored with a warning, because a
provider's readers are fixed when it is built. The endpoint is used as given, so include the
path (`https://collector:4318/v1/metrics`, not `https://collector:4318`). The interval setting
always applies, so `OTEL_METRIC_EXPORT_INTERVAL` has no effect. The Runtime flushes its meter
provider when the API shuts down.

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
   - The Runtime can send all of its egress through such a proxy; see below.

### Egress proxy

`network.egress_proxy` sends every outbound client the Runtime builds through one HTTP(S)
proxy: `core.http`, declared `http_tools`, `remote_tool`, MCP streamable HTTP and SSE
transports, MCP OAuth discovery and token requests, OIDC discovery and JWKS fetches, workflow
notifications, `HTTPModelService` (when built with the container's `outbound`), and the HTTP
model providers (OpenAI, Azure OpenAI, Anthropic, Mistral, Gemini, xAI, Groq, DeepSeek,
Hugging Face, Ollama and `openai_compatible`).

```yaml
network:
  egress_proxy:
    url: http://egress-proxy.internal:3128   # or env://EGRESS_PROXY_URL; no credentials here
    username: env://EGRESS_PROXY_USER        # optional, sent as Proxy-Authorization (Basic)
    password: env://EGRESS_PROXY_PASSWORD
    no_proxy: [localhost, 127.0.0.0/8, .svc.cluster.local]   # reached directly
    resolve_targets: true
```

- `https://` targets go through a CONNECT tunnel, so TLS stays end to end between the Runtime
  and the target (and `network.remotes` identities and pins still apply); `http://` targets are
  forwarded to the proxy.
- The target is checked before the proxy is contacted, exactly as for a direct connection:
  scheme, `security.allowed_http_hosts`, resolution, and private, link-local and metadata
  addresses. A refused target never reaches the proxy. The proxy host itself may be private
  because it is configured explicitly, and the proxied connection pool can reach nothing else.
- **Limit.** The proxy makes the final connection and resolves the name again, so the
  Runtime's DNS-rebinding pinning protects only up to the proxy. Give the proxy its own egress
  policy: deny private, link-local and metadata ranges and allow only the destinations you
  expect. If the Runtime cannot resolve external names at all, set `resolve_targets: false`;
  it then checks only the allowlist and IP literals and the proxy must enforce the rest.
- Credentials never appear in the proxy URL, logs, `repr` or errors. A `proxy=` passed to
  `create_safe_http_client` must be the configured proxy (otherwise `PolicyDeniedError`), and
  `mounts=` is refused.
- Model provider clients use the proxy only when `network` is configured; otherwise they keep
  `httpx`'s defaults, including `HTTPS_PROXY`. The S3 client, OTLP and Traccia exporters and
  vector-store SDKs are not covered and keep their own proxy settings (normally the
  `HTTPS_PROXY` environment variable).

Hosts that embed the Runtime can build the same clients: `build_outbound_network(settings.network,
environment)` from `algen_agent_runtime.security.outbound`, then
`create_safe_http_client(..., outbound=...)` (checked) or `create_outbound_http_client(outbound)`
(operator-configured endpoints). A built container exposes it as `container.outbound`.

### Remote service identity: mTLS, private CAs and pinning

`network.remotes` sets TLS per remote host (or `*.domain` for its subdomains) for every client
above, direct or through the proxy:

```yaml
network:
  remotes:
    api.partner.example:
      ca_bundle: /etc/algen/partner-ca.pem          # replaces the default trust store
      client_certificate: env://PARTNER_CLIENT_CERT   # PEM text, or a file path
      client_key: env://PARTNER_CLIENT_KEY            # PEM text, or a file path
      client_key_password: env://PARTNER_KEY_PASSWORD # only for an encrypted key
      pinned_sha256:
        - "5F:3A:...:9C"                              # leaf certificate SHA-256, colons optional
    "*.mcp.internal.example":
      ca_bundle: env://INTERNAL_CA_PEM
```

- A `ca_bundle` replaces the default CAs for that host; certificate and host name verification
  stay on. Pins are checked in addition and close the connection with `PolicyDeniedError` on
  mismatch. Compute a pin with
  `openssl x509 -in server.pem -outform der | openssl dgst -sha256`.
- Keys and certificates are file paths or `env://` references; literal PEM in YAML is refused.
  A key from `env://` is written to a private (mode 600) temporary file only while `ssl` loads
  it and removed straight away. Load errors name the remote, never the key material.
- Use the host of a model provider's `base_url`, an MCP server's `url`, an HTTP tool's URL or
  an OIDC issuer as the key.

## Subprocess and MCP stdio sandboxing

MCP stdio servers and `core.subprocess` run as child processes of the Runtime.
`security.sandbox` (or a stdio server's own `sandbox`, which replaces it for that server)
controls them:

| Setting | Default | Effect |
|---|---|---|
| `env_allowlist` | unset | Names of the Runtime's environment variables a child inherits. Unset: stdio servers keep `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM`, `TEMP`, `TMP`, `SYSTEMROOT` (what they always had) and `core.subprocess` inherits nothing. A server's explicit `env` is always passed. |
| `working_directory` | unset | Working directory for children that set none (`cwd`). |
| `limits` | none | POSIX `setrlimit` values: `cpu_seconds`, `memory_bytes` (address space; Linux only, macOS refuses it), `open_files`, `processes` (counts all of the user's processes) and `file_size_bytes`. A value above the Runtime's hard limit is capped at it. |
| `wrapper` | none | A command prefix that isolates the child's filesystem and network: `bwrap`, `nsjail`, `firejail`, `sandbox-exec`. The command and its arguments follow it. |
| `required` | `false` | Refuse to start any stdio server or `core.subprocess` call without a `wrapper` (checked before the MCP package is imported). |
| `subprocess_timeout_seconds` | 60 | `core.subprocess` wall-clock limit. |

Children always start in a new session. On timeout or cancellation `core.subprocess` kills
the whole process group (with any background processes the command started); on shutdown the
MCP SDK terminates the stdio server's process group (SIGTERM, then SIGKILL after two seconds).
With limits, an `env_allowlist` or a wrapper set, the child is started through a small
standard-library launcher (`python -I -S .../_sandbox_exec.py`) that applies the limits,
removes every variable not on the list and then `exec`s the wrapper or command, so the limits
hold for everything the child starts. The defaults change nothing for existing configurations.

Limits and a scrubbed environment are not isolation: without a wrapper a child reads every
file and reaches every host the Runtime's user can. A Linux example with bubblewrap, which
gives an MCP server a read-only system, a private `/tmp`, no network and no view of the
Runtime's files:

```yaml
security:
  sandbox:
    required: true
    env_allowlist: [PATH, LANG]
    limits:
      cpu_seconds: 600
      memory_bytes: 2147483648     # 2 GiB
      open_files: 1024
      file_size_bytes: 104857600   # 100 MiB
    wrapper: [/usr/bin/bwrap,
              --ro-bind, /usr, /usr,
              --symlink, usr/lib, /lib,
              --symlink, usr/lib64, /lib64,
              --symlink, usr/bin, /bin,
              --ro-bind, /opt/mcp-servers, /opt/mcp-servers,
              --proc, /proc, --dev, /dev, --tmpfs, /tmp,
              --unshare-all, --die-with-parent, --new-session,
              --]
mcp_servers:
  - name: files
    transport: stdio
    command: /opt/mcp-servers/files/bin/server
    cwd: /tmp
  - name: web_search            # needs the network: its own policy replaces the default
    transport: stdio
    command: /opt/mcp-servers/search/bin/server
    sandbox:
      env_allowlist: [PATH]
      limits: {cpu_seconds: 600, open_files: 1024}
      wrapper: [/usr/bin/bwrap, --ro-bind, /usr, /usr, --symlink, usr/lib, /lib,
                --symlink, usr/lib64, /lib64, --symlink, usr/bin, /bin,
                --ro-bind, /opt/mcp-servers, /opt/mcp-servers,
                --ro-bind, /etc/resolv.conf, /etc/resolv.conf,
                --ro-bind, /etc/ssl, /etc/ssl,
                --proc, /proc, --dev, /dev, --tmpfs, /tmp,
                --unshare-all, --share-net, --die-with-parent, --]
```

bubblewrap needs unprivileged user namespaces (in a container, allow them in its seccomp or
security profile). Equivalent prefixes: `[nsjail, --config, /etc/algen/mcp.cfg, --]`,
`[firejail, --quiet, --net=none, --private, --]` and, on macOS,
`[sandbox-exec, -f, /etc/algen/mcp.sb]`. Avoid `limits.processes` with a wrapper that
creates namespaces unless you have measured what it needs.

## Plugin signing

Plugins and workflow hook packages can be signed with Ed25519, and the Runtime verifies them
before importing any of their code. Install the `encryption` extra (it provides
`cryptography`).

```bash
algen-agent-runtime plugin keygen signing-key.pem          # prints the public key
algen-agent-runtime plugin sign src/acme_plugins --publisher acme \
    --key env://ACME_PLUGIN_SIGNING_KEY --name crm --version 1.2.0
algen-agent-runtime plugin verify acme_plugins.crm:CrmPlugin --config agent.yaml
```

```yaml
security:
  trusted_plugin_prefixes: [acme_plugins]
plugins:
  trusted_publishers:
    acme: ["MCowBQYDK2VwAyEA..."]   # base64 raw public keys; several allow rotation
  require_signed: true
```

`plugin sign` writes `algen-plugin-signature.json` into the package directory. It signs the
publisher, the bound name and version, the package name and the SHA-256 of every file in the
package (not `__pycache__` or `.pyc`), so any changed, added or removed module, data file or
extension module fails verification. Sign as the last build step, after anything that writes
into the package, and keep the private key in CI secrets.

At load time the package is located with the import finders, without executing it, and
verified. With `require_signed: true` an unsigned package or one from a publisher not in
`trusted_publishers` is refused even when its prefix is trusted, for `domain_plugins` and for
workflow `hook_provider` modules alike; after import the plugin's manifest must match the
signed publisher, name and version. Without `require_signed`, signatures from trusted
publishers are still verified and a bad one refuses the plugin; unsigned plugins load as
before. The signature does not cover the plugin's dependencies or bytecode in `__pycache__`,
and does not stop a file being replaced after verification, so install plugin packages
read-only (for example in the container image). `PluginManifest.signature` is not used.


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

The persistence drills (restart, outage, failover, backup/restore, upgrade/rollback, soak) are
described in [Persistence: restarts, outages, backup and rollback](#persistence-restarts-outages-backup-and-rollback).

Back up each durable store enabled by the application according to its retention policy (see
[Backup and restore](#backup-and-restore)). User deletion must remove tenant/session memory and authorized artifacts while preserving legally required, redacted audit records.

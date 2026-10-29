# Production-readiness roadmap

Owner: AlgenAI Architecture & Security

Status: Active tracking — pre-production

Last reviewed: 2026-10-29 (Runtime `0.1.0a17` and unreleased changes)

Algen Agent Runtime is a restart-aware development runtime with durable execution, governance,
security, and operability primitives. The capabilities below are implemented in the current
repository, but their presence does not by itself establish a hardened production service. Release
status and unreleased changes are tracked separately in `CHANGELOG.md`.

## Implemented and verified baseline

- Configuration-driven stores with fail-fast backend and secret-reference validation.
- PostgreSQL run, conversation memory, event, immutable audit, approval, artifact, and tool-execution
  stores sharing a managed async connection pool.
- Tenant-scoped, optimistically versioned multi-agent workflow checkpoints with manifest
  fingerprints, pause/resume without predecessor replay, and explicit fail-closed or retry recovery.
- First-class workflow approval nodes with attributed decisions, schema-validated modifications,
  bounded expiry, safe rejection propagation, and content-minimal lifecycle events.
- Exact-version parent/child workflow dispatch with independent linked checkpoints, cycle/depth
  limits, nested human-wait propagation, and recovery that reconciles recorded children.
- Validated email contracts and a TLS-first SMTP adapter integrated with the durable side-effect
  ledger; provider-specific delivery reconciliation remains application or connector work.
- Transactional schema tracking, forward migrations, and initialization.
- Recovery of unfinished model, tool, verification, retry, and composition checkpoints.
- Persistent tool reservations, cached completed results, and indeterminate side-effect detection.
- Graceful drain with resumable checkpoints for work interrupted during shutdown.
- JWT signature, expiry, issuer, and audience validation with tenant and scope claims.
- Event subscriber backpressure that cannot terminate an agent run.
- Batched, concurrency-safe maintenance sweeps that expire stale paused runs and enforce opt-in
  retention for terminal runs and audit events, verified against live PostgreSQL.
- Policy-transformed request, model, retrieval, tool, memory, and final-response values are propagated.
- Typed analytical DAGs with separate results, schema checks, retries, budgets, and checkpoints.
- Versioned analytical methods and non-LLM model-service discovery, health, drift, and fallback.
- Query trust/governance, fingerprints, scoped result caching, and deterministic evaluation gates.
- Idempotent work enqueue, in-memory and PostgreSQL queues, `SKIP LOCKED` claims, explicit lease
  renewal, lease-owner fencing, bounded retries, and a single-item distributed worker execution path.
- Cross-process durable event polling for PostgreSQL-backed event subscribers.
- Tenant-scoped artifact upload, integrity metadata, scan/quarantine state, expiry, listing, and
  deletion, including staged artifacts that exist before a run.
- PostgreSQL-metadata/S3-blob artifact storage with encryption and checksum requests, tenant-hashed
  keys, compensating upload cleanup, bounded retention, and an audited compare-and-set scanner boundary.
- Outbound HTTP security with single-resolution address validation, direct TCP socket pinning to
  pre-validated IPs, fail-closed multi-address checks, disabled redirects, and TLS SNI preservation
  across HTTP tools, remote MCP transports, and HTTP model services.
- Remote MCP bearer authentication and secret headers resolved through `env://` references, with
  literal sensitive headers rejected from committed configuration.
- Tenant-aware, single-node API admission rate limiting and in-flight run-creation request quotas with
  token-bucket refills, burst headroom, route-class thresholds, and HTTP 429 `Retry-After` handling.
- Streamed request-body and artifact wire-size limits that reject oversized or invalidly framed requests.
- Baseline liveness and readiness endpoints that report model-provider health and probe registered
  resources exposing `health()` or `ping()`.
- CI across Python 3.12 and 3.13, base and optional-dependency test jobs, package and clean-wheel
  installation smoke tests, artifact checksums, dependency review, CodeQL, Bandit, Gitleaks, and
  container vulnerability scanning.

Added since the 2026-10-11 review (`0.1.0a11` to `0.1.0a15`):

- Tool-call guards kept in checkpointed run state: arguments checked against the tool's input schema
  and an optional `Tool.precheck` before any approval is created, per-tool refusal limits, a repeat
  guard, per-tool call limits, and a wrap-up notice near the step or token budget.
- A `UsageMeter` around every model dispatch (steps, retries, fallbacks, repairs, streams,
  compaction) that is told the tenant, user, run, provider and model of each call (`MeterCall`);
  refusals end the run without retry, fallback or provider blame.
- Per-run caller credentials for tools, held in memory only, cleared when a run pauses or ends, and
  never written to run state, stores, events, audits, spans, logs, caches or exception text.
- OAuth for remote MCP servers with PKCE, single-use flow state, per-tenant or per-user tokens, and an
  AES-256-GCM-encrypted PostgreSQL token store.
- Declared plugins loaded only from `security.trusted_plugin_prefixes` matched on module boundaries,
  with a startup component check and a static `check`/`describe` inventory.
- Hidden-content and prompt-injection inspection for PDF, DOCX, XLSX, PPTX, HTML, CSV and text
  documents.
- Traccia receives only traces the Runtime started; foreign HTTP and instrumentor spans are dropped by
  default.

## P0 before a hardened single-node production claim

Items 3, 4 and 6 are complete for the single-node scope as of `0.1.0a17`, and item 1 in the
unreleased changes; each lists what is left for later. Items 2, 5 and 7 remain open.

1. **Close persistence validation gaps.** *Done (single node, unreleased):* a drill suite
   (`tests/drills`, marker `drill`, run in CI by the `persistence-drills` job on throwaway
   PostgreSQL 17 and Redis 7 containers) builds real Runtime processes on live stores and covers
   first start on an empty database, restart on a migrated one and four concurrent first starts;
   SIGKILL crashes mid-model-call and mid-tool-call recovered by a new process with one ledger
   record and one external effect per run, a run paused for approval, a workflow run and conversation
   turns across a crash, and five crashes in a row; two processes deciding one approval at once
   (one wins, the other gets 409/`ConflictError`, the run resumes once); PostgreSQL and Redis paused
   under load (readiness down, new work refused with `StoreUnavailableError`/503 within bounded
   timeouts, the run in flight kept and driven again once the store is back, no restart); a
   database restart as a single-node failover with no committed state lost; `pg_dump` of a live
   database restored into a new one where the paused run resumes with its approvals, ledger and
   events; schema upgrade from the `0.1.0a17` migration set and from data `0.1.0a17` itself wrote,
   and rollback to `0.1.0a17` on a database the current code migrated and wrote; and a bounded soak
   (marker `soak`, off by default) of runs, approvals and turns at concurrency. Fixed on the way:
   unbounded PostgreSQL/Redis calls (connect and command timeouts), raw driver errors (now
   `StoreUnavailableError`, HTTP 503), runs failed by a transient outage (now kept and resumed), one
   failure per dead pooled connection after a restart (the pool is renewed), a drive superseded by
   another failing the run (it stands down), step budget used up by repeated recovery, a resume right
   after the pause consuming the approval without driving the run, and a `run_store` other than
   PostgreSQL accepted with PostgreSQL stores whose rows reference runs. The runbook is in
   [operations](operations.md#persistence-restarts-outages-backup-and-rollback). *Later:*
   coordinated recovery across processes (leader election) and multi-replica drills; true HA
   failover (replica promotion, asynchronous-replication loss) against a managed or replicated
   PostgreSQL; S3 artifact restore drills; workflow agent nodes that reattach to their interrupted
   run instead of retrying it; a published long-running soak (hours) with load and noisy-neighbor
   tenants; resuming outage-interrupted workflow and conversation drives in process (they are
   recovered by the next start).
2. **Harden policy distribution and coverage.** Workflow approval and clarification outcomes are
   implemented. Tool calls are now refused before approval when their arguments fail the input schema
   or a precheck, and documents in seven formats are inspected for hidden content and injection.
   Store-level tenant isolation is tested for PostgreSQL, Redis, retrieval, cache, workflow
   checkpoints, MCP OAuth tokens and the API. *Remaining:* signed policy bundles, explicit
   fail-open/fail-closed behavior at every remaining policy boundary, and an adversarial suite for
   prompt injection, data loss, PII, secrets, and cross-tenant access through agent behavior rather
   than store calls.
3. **Complete side-effect operations.** *Done (`0.1.0a17`):* a tenant-scoped reconciliation API
   (`runtime.reconciliation`, `/v1/tool-executions`, scope `tools:reconcile`) lists indeterminate and
   stale started calls; operators resolve them as completed or failed with actor and reason, audited
   and announced; an optional `Tool.reconcile` hook serves operator reconciliation and replay-time
   reconciliation; `Tool.compensation` runs only on an explicit operator request, at most once
   (`compensating` to `compensated` or `compensation_failed`, migration 014); the opt-in
   `tool_policy.on_indeterminate: pause` resumes a run once its call is settled; and the
   ledger-before-checkpoint write order is documented and covered by crash-between-writes tests on
   in-memory and PostgreSQL stores. *Later:* reconcile hooks for built-in HTTP, email and MCP tools;
   reconciliation pauses for workflow tool steps; a configurable stale threshold; single-transaction
   coupling if the executor and run store ever share a connection.
4. **Finish authentication options.** *Done (`0.1.0a17`):* `auth_mode: oidc` verifies bearer JWTs
   against issuer JWKS (discovery or `jwks_url`, fetched through the SSRF-safe client from
   allowlisted hosts, TTL cache, rate-limited refresh on an unknown `kid`, last good keys kept,
   asymmetric algorithms bound to the key type, bounded skew, optional maximum token lifetime);
   per-issuer claim mapping to tenant and scopes that fails closed; tenant-scoped revocation (`jti`
   denylist and per-subject cutoff, memory or PostgreSQL, admin routes); a trusted-proxy
   client-certificate identity hook, off by default; and content-minimal authentication audit
   records, metric and log. Header identity remains loopback-only development mode. *Later:* token
   introspection (RFC 7662); mTLS terminated by the Runtime itself mapped to principals; purging
   expired revocations in the maintenance sweep.
5. **Harden network, plugins, and local execution.** Keep outbound redirects disabled; if redirect
   support is introduced, validate and pin every hop. Add egress proxy hooks, sandbox subprocess and
   MCP stdio tools (which currently execute as trusted, unsandboxed host code under minimal environment
   filtering), add stronger remote service identity where required, verify plugin signatures, and
   prevent untrusted in-process plugin loading. Done: redirects stay disabled; plugins and workflow
   hooks load only from allowlisted module prefixes matched on module boundaries, checked before
   import. *Remaining:* egress proxy hooks (there is no deployment setting for one, and a `proxy`
   passed to `create_safe_http_client` is not validated against IP pinning), subprocess and stdio
   MCP sandboxing, stronger remote service identity, and verification of `PluginManifest.signature`,
   which is declared but never checked.
6. **Strengthen readiness and lifecycle.** *Done (`0.1.0a16`):* `/health/ready` reports every store,
   cache, model provider, retriever, MCP server, telemetry exporter and health-capable resource with
   required, status and a content-minimal reason, plus `degraded_dependencies`, and fails only when
   a required dependency is down or the service drains (`runtime.readiness`); container start
   recovers the workflow runs the HTTP workflow service owns, with the exact manifest and trusted
   hooks; the maintenance sweep expires paused workflow approval and input waits through the
   engine's expiry path; shutdown drains workflows, conversations and runs and leaves interrupted
   work recoverable; repeated start/drain/restart cycles are tested on in-memory stores and live
   PostgreSQL. *Later:* exporter health probes (OTLP and Traccia report `unknown`); a
   `maintenance --workflows` CLI option; HTTP model services in readiness; a closed database that
   refuses reuse.
7. **Bound every resource.** Request wire sizes, artifact sizes, API request rates, and in-flight
   run-creation requests are bounded for single-node deployments. The opt-in maintenance sweep purges
   expired memory and artifacts and deletes terminal runs (with events, approvals, and tool records)
   and audit events past configured retention windows. Within a run, `max_calls_per_tool`, the repeat
   guard and the wrap-up notice bound tool use, and a host `UsageMeter` can enforce per-tenant model
   quotas. *Remaining:* per-run limits for model responses and event counts, per-tenant storage quotas,
   and retention for conversations, workflow checkpoints, analytical graphs, and work items.

## Remaining for hardened multi-worker or horizontally scaled use

The repository already provides queue contracts, in-memory and PostgreSQL queue implementations,
lease renewal and owner fencing primitives, bounded retries, and `DistributedWorker.run_once()`.
Hardened multi-worker operation still requires:

- Analytical dispatch integrated into the service lifecycle, a production worker CLI, an automatic
  heartbeat loop, versioned fencing tokens, drain/rebalance behavior, and dead-letter/operator APIs.
- PostgreSQL notifications or a broker where polling is insufficient, plus atomic event sequence
  allocation under concurrent publishers.
- Distributed rate limits, circuit breakers, concurrency limits, and cancellation delivery.
- Leader election for recovery and maintenance work.
- Distributed workflow claims/fencing so only one replica can drive a recoverable workflow at a time.
- The PostgreSQL MCP OAuth token store for any deployment with more than one worker (the in-memory
  store cannot see a flow started on another worker); caller credentials are per process, so a run
  resumed on another worker needs them passed to `resume` again.
- Live S3-compatible target testing with fault injection, multipart/large-object strategy, lifecycle
  reconciliation, and restore drills.

## Release confidence and operability

Implemented release evidence includes supported-Python CI, package construction and inspection,
SHA-256 checksums, clean wheel installation and scaffold smoke tests, security and dependency scans,
container image build/scanning, and PyPI trusted publishing through OIDC.

Remaining release and operational gates:

- Reproducible dependency lock files, SBOM and license reports, signed tags/releases or equivalent
  provenance attestations, and a container runtime smoke test rather than build/scan alone.
- Provider sandbox contracts plus load and noisy-neighbor tests. Crash-recovery, store
  fault-injection (outage, failover) and a bounded soak run as persistence drills (`tests/drills`);
  a long-running published soak remains.
- Golden telemetry/redaction tests across model, tool, retrieval, approval, verification, and failures.
- SLOs, capacity guidance, dashboards, alerts, runbooks, profiling, and benchmark baselines.
- Stable extension APIs and explicit compatibility levels for framework adapters; observation
  compatibility is not equivalent to lifecycle control.

## Production gate

Do not call a release production-ready until automated tests demonstrate restart-safe approvals and
tool execution, cross-tenant isolation, authenticated access, bounded resource use, provider
cancellation, schema upgrade and rollback, redacted telemetry, and recovery under repeated process
failure.

Evidence so far: restart-safe approvals and tool execution, schema upgrade and rollback (against
`0.1.0a17`) and recovery under repeated process failure are covered by the persistence drills in
`tests/drills` on live PostgreSQL and Redis.

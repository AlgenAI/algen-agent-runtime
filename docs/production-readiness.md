# Production-readiness roadmap

Owner: AlgenAI Architecture & Security

Status: Active tracking — pre-production

Last reviewed: 2026-10-30 (Runtime `0.1.0a18`)

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

Items 3, 4 and 6 are complete for the single-node scope as of `0.1.0a17`, and items 1, 2, 5 and 7 as
of `0.1.0a18`; each lists what is left for later. No P0 item remains open.

1. **Close persistence validation gaps.** *Done (single node, `0.1.0a18`):* a drill suite
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
2. **Harden policy distribution and coverage.** *Done (`0.1.0a18`):* policy bundles
   (`policies.bundles`) carry declarative rules (denied tools, tools that need approval, deny and
   redact patterns per boundary) with their name, version, issue and expiry times inside a
   detached Ed25519 signature verified at start-up against `env://` or `file://` trusted keys;
   `policies.require_signed` refuses to start on an unsigned bundle, and a signature that does not
   verify, an expired or future-dated bundle, or a version below `min_version` (rollback) is always
   refused; bundle policies run for every agent; `algen-agent-runtime policy keygen|sign|verify`.
   Every policy boundary (`input`, `before_retrieval`, `after_retrieval`, `before_model`,
   `after_model`, `plan_tool`, `before_tool`, `after_tool`, `final_response`,
   `before_memory_write`, `after_memory_write`) has an explicit fail mode when a policy raises or
   times out (`policies.on_error`, `policies.boundary_on_error`,
   `policies.evaluation_timeout_seconds`), `closed` by default everywhere as before, with a
   `policy.error` audit record, the `algen_agent_runtime.policy.errors` metric and a type-only log
   line. `tests/adversarial` runs scripted attacks through agent behaviour (prompt injection, data
   loss, PII and secrets, cross-tenant access) in normal CI; the gaps it found are fixed (a deny at
   `plan_tool` was ignored, a deny could be shadowed by an earlier approval requirement, a reused
   tool-call id could inherit an approval, bearer tokens and card numbers were not redacted, and
   credentials leaked through approval parameters, runtime error text and opt-in telemetry
   content). Workflow approval and clarification outcomes, tool-call guards, document inspection
   and store-level tenant isolation were done earlier. *Later:* a persisted version high-water mark
   (rollback protection is the operator's `min_version`); fetching bundles from a remote source
   and reloading them without a restart; a key revocation list beyond editing `trusted_keys`; fail
   modes for custom (non-`CompositePolicyEngine`) engines, which keep their own error behaviour;
   run events for the tool executor's boundaries (they are audited, not announced); a
   model-based injection classifier in place of the deterministic patterns; the adversarial suite
   against live providers and PostgreSQL stores; verifying `PluginManifest.signature` (item 5).
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
5. **Harden network, plugins, and local execution.** *Done (`0.1.0a18`):* outbound redirects stay
   disabled; plugins and workflow hooks load only from allowlisted module prefixes, checked before
   import. `network.egress_proxy` sends every client the Runtime builds (HTTP tools, MCP HTTP
   transports and OAuth, OIDC discovery and JWKS, workflow notifications, `HTTPModelService` and
   the HTTP model providers) through one proxy, CONNECT for `https://`, after the target passed the
   allowlist and private-network checks; the proxied pool reaches only the proxy, credentials are
   `env://` references kept out of URLs, logs and errors, and a different caller `proxy=` (or
   `mounts=`) is refused. `network.remotes` adds per-host CA bundles, mTLS client certificates
   (file or `env://`) and pinned server certificate fingerprints, through the proxy too.
   `security.sandbox` (or a stdio server's `sandbox`) sets an environment allowlist, working
   directory, POSIX resource limits and an operator wrapper (`bwrap`, `nsjail`, ...) for MCP stdio
   servers and `core.subprocess`, applied by a standard-library launcher before `exec`; children
   run in their own session and their process group is killed on timeout or shutdown;
   `required: true` refuses to start a process without a wrapper. Defaults change nothing.
   Plugin and hook packages can carry an Ed25519 signature over their identity and every file,
   verified against `plugins.trusted_publishers` before import; `plugins.require_signed` refuses
   unsigned or untrusted packages under trusted prefixes; `algen-agent-runtime plugin
   keygen|sign|verify`. *Later:* the proxy, not the Runtime, makes the final connection, so DNS
   pinning ends at the proxy (the proxy must enforce its own destination policy); the S3 client,
   OTLP and Traccia exporters, vector-store SDKs and the TypeSafe decision client keep their own
   proxy settings; plugins that build `HTTPModelService` must pass `outbound` themselves; SOCKS
   proxies; a Runtime-managed kernel sandbox (seccomp, cgroups) instead of an operator wrapper,
   and resource limits on Windows; a startup timeout for stdio servers; plugin signatures over
   dependencies and bytecode, publisher key revocation, and protection against files replaced
   between verification and import (install read-only).
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
7. **Bound every resource.** *Done (`0.1.0a18`):* request wire sizes, artifact sizes, API request rates, and
   in-flight run-creation requests are bounded for single-node deployments. Within a run,
   `max_calls_per_tool`, the repeat guard and the wrap-up notice bound tool use, and a host
   `UsageMeter` can enforce per-tenant model quotas. Each run is limited by `budget.max_model_calls`
   (1,000; extended by a continuation, counted by the wrap-up notice), `budget.max_response_chars`
   per model response (1,000,000 characters of text plus tool-call arguments, enforced while a
   response streams) and `budget.max_events` (100,000; past it only events that end or pause the run
   are recorded and the run fails at its next step); streamed text is recorded as at most one
   `model.delta` per `runtime.stream_delta_interval_ms` (50 ms). A run that reaches a limit fails
   with `run.failed` `reason: limit_exceeded` and a `run.limit_exceeded` audit record.
   `storage.quotas` sets per-tenant item and byte limits for artifacts, session memory and learned
   memory (unlimited by default, with per-tenant overrides), enforced on write in the in-memory,
   PostgreSQL (trigger-maintained usage table, row-locked check in the write's transaction,
   migration 016), S3 and Redis stores with `QuotaExceededError` (HTTP 413, `QUOTA_EXCEEDED`);
   `GET /v1/storage/usage` reports a tenant's usage. The opt-in maintenance sweep purges expired
   memory and artifacts and deletes terminal runs (with events, approvals, and tool records), audit
   events, idle conversations (with messages, events and feedback), finished workflow checkpoints,
   finished analytical graphs and completed or dead work items past configured retention windows,
   on in-memory and PostgreSQL stores. *Later:* quotas on conversations and their messages and on
   run and audit records; a per-conversation event limit; a total output limit per run beyond the
   token budget; Redis quota accounting on a clustered Redis; distributed (cross-replica) limits.

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

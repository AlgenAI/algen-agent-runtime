# Production-readiness roadmap

Owner: AlgenAI Architecture & Security

Status: Active tracking — pre-production

Last reviewed: 2026-10-11

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

## P0 before a hardened single-node production claim

1. **Close persistence validation gaps.** CI runs storage integration tests against live PostgreSQL 16,
   including schema migrations, run state, optimistic conflicts, artifacts, tool deduplication, and
   workflow checkpoints. CI also runs the Redis run, memory, and cache stores against live Redis 7
   through `REDIS_URL`, including WATCH-conflict handling.
   Complete startup, restart recovery, concurrent approval, backup/restore, database-unavailability,
   failover, and soak drills against supported deployment topologies.
2. **Harden policy distribution and coverage.** Workflow approval and clarification outcomes are
   implemented. Add signed policy bundles, explicit fail-open/fail-closed behavior at every remaining
   policy boundary, and adversarial tests for prompt injection, data loss, PII, secrets, and
   cross-tenant access.
3. **Complete side-effect operations.** Add reconciliation APIs, operator resolution of indeterminate
   calls, compensation execution, and transactional coupling between tool records and run checkpoints
   where the backing system permits it.
4. **Finish authentication options.** Add OIDC/JWKS rotation and caching, mTLS/service identity hooks,
   authorization policy mapping, token-revocation strategy, and authentication audit events. Header
   identity mode must remain explicitly development-only.
5. **Harden network, plugins, and local execution.** Keep outbound redirects disabled; if redirect
   support is introduced, validate and pin every hop. Add egress proxy hooks, sandbox subprocess and
   MCP stdio tools (which currently execute as trusted, unsandboxed host code under minimal environment
   filtering), add stronger remote service identity where required, verify plugin signatures, and
   prevent untrusted in-process plugin loading.
6. **Strengthen readiness and lifecycle.** The baseline probes model providers and health-capable
   registered resources. Make mandatory dependency declarations explicit; cover every required store,
   retriever, model, MCP server, and telemetry exporter; expose degradation reasons; wire
   application-owned workflow recovery into each service lifecycle; expire paused workflow approvals
   without user traffic (paused agent runs are expired by the maintenance sweep); and test repeated
   start/drain cycles.
7. **Bound every resource.** Request wire sizes, artifact sizes, API request rates, and in-flight
   run-creation requests are bounded for single-node deployments. The opt-in maintenance sweep purges
   expired memory and artifacts and deletes terminal runs (with events, approvals, and tool records)
   and audit events past configured retention windows. Add per-run limits for model responses and
   event counts, per-tenant storage quotas, and retention for conversations, workflow checkpoints,
   analytical graphs, and work items.

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
- Live S3-compatible target testing with fault injection, multipart/large-object strategy, lifecycle
  reconciliation, and restore drills.

## Release confidence and operability

Implemented release evidence includes supported-Python CI, package construction and inspection,
SHA-256 checksums, clean wheel installation and scaffold smoke tests, security and dependency scans,
container image build/scanning, and PyPI trusted publishing through OIDC.

Remaining release and operational gates:

- Reproducible dependency lock files, SBOM and license reports, signed tags/releases or equivalent
  provenance attestations, and a container runtime smoke test rather than build/scan alone.
- Provider sandbox contracts plus load, soak, fault-injection, crash-recovery, and noisy-neighbor tests.
- Golden telemetry/redaction tests across model, tool, retrieval, approval, verification, and failures.
- SLOs, capacity guidance, dashboards, alerts, runbooks, profiling, and benchmark baselines.
- Stable extension APIs and explicit compatibility levels for framework adapters; observation
  compatibility is not equivalent to lifecycle control.

## Production gate

Do not call a release production-ready until automated tests demonstrate restart-safe approvals and
tool execution, cross-tenant isolation, authenticated access, bounded resource use, provider
cancellation, schema upgrade and rollback, redacted telemetry, and recovery under repeated process
failure.

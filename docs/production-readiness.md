# Production-readiness roadmap

Owner: AlgenAI Architecture & Security  
Status: Active tracking  
Last reviewed: 2026-09-22  

Algen Agent Runtime is a restart-aware development runtime. It now has configurable
PostgreSQL persistence, Redis checkpoint/memory options, a tool side-effect ledger, startup recovery,
graceful draining, bounded event fan-out, verified JWT authentication, analytical graph checkpoints,
and PostgreSQL worker leases. These are necessary primitives, not by themselves a hardened production
service.

## Implemented in the durable single-node increment

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
  ledger; provider-specific delivery reconciliation remains application/connector work.
- Transactional schema tracking and initialization.
- Recovery of unfinished model, tool, verification, retry, and composition checkpoints.
- Persistent tool reservations, cached completed results, and indeterminate side-effect detection.
- Graceful drain with resumable checkpoints for work interrupted during shutdown.
- JWT signature/expiry/issuer/audience validation with tenant and scope claims.
- Event subscriber backpressure that cannot terminate an agent run.
- Policy-transformed request, model, retrieval, tool, memory, and final-response values are propagated.
- Typed analytical DAGs with separate results, schema checks, retries, budgets and checkpoints.
- Versioned analytical methods and non-LLM model-service discovery, health, drift and fallback.
- Query trust/governance, fingerprints, scoped result caching and deterministic evaluation gates.
- Idempotent work enqueue, PostgreSQL `SKIP LOCKED` leases and cross-process durable event polling.
- Tenant-scoped artifact upload, integrity metadata, scan/quarantine state, expiry, listing, and
  deletion, including staged artifacts that exist before a run.
- PostgreSQL-metadata/S3-blob artifact storage with encryption and checksum requests, tenant-hashed
  keys, compensating upload cleanup, bounded retention, and an audited compare-and-set scanner boundary.

## P0 before a hardened single-node production claim

1. **Exercise real infrastructure.** Add containerized PostgreSQL and Redis integration tests covering
   startup, schema initialization, restart recovery, concurrent approval decisions, artifact limits,
   backup/restore, and database unavailability. Current offline tests validate behavior and wiring but
   do not exercise a live database.
   The offline workflow-store conformance suite now checks detached reads, tenant isolation,
   optimistic conflicts, recoverable filtering, and corrupt-state failure for memory and PostgreSQL
   implementations; live PostgreSQL fault injection is still required.
2. **Harden policy semantics.** Extend approval and clarification outcomes across every declared
   policy boundary, signed policy bundles, explicit fail-open/fail-closed behavior, and adversarial tests for
   prompt injection, data loss, PII, secrets, and cross-tenant access.
3. **Complete side-effect operations.** Add reconciliation APIs, operator resolution of indeterminate
   calls, compensation execution, and transactional coupling between tool records and run checkpoints
   where the backing system permits it.
4. **Finish authentication options.** Add OIDC/JWKS rotation and caching, mTLS/service identity hooks,
   authorization policy mapping, token-revocation strategy, and authentication audit events. Header
   identity mode must remain explicitly development-only.
5. **Harden network and plugins.** Bind HTTP connections to validated addresses to mitigate DNS
   rebinding, revalidate redirects, add egress proxy hooks, sandbox subprocess tools, authenticate
   remote tools, verify plugin signatures, and prevent untrusted in-process plugin loading.
6. **Strengthen readiness and lifecycle.** Probe every mandatory store, model and telemetry dependency;
   wire application-owned workflow recovery into each service lifecycle, expire paused runs without
   user traffic, add health degradation reasons, and test repeated start/drain cycles.
7. **Bound every resource.** Enforce limits for context, responses, events, memory, audit metadata,
   concurrent runs, tenant quotas, and database growth. Add cleanup jobs for expired memory and stale
   operational records.

## Remaining for hardened multi-worker or horizontally scaled use

- Integrate analytical dispatch into the service lifecycle and provide a production worker CLI,
  heartbeat loop, fencing tokens, drain/rebalance behavior, and dead-letter/operator APIs.
- Replace polling where necessary with PostgreSQL notifications or a broker and make event sequence
  allocation atomic under concurrent publishers.
- Distributed rate limits, circuit breakers, concurrency limits, and cancellation delivery.
- Leader election for recovery and maintenance work.
- Distributed workflow claims/fencing so only one replica can drive a recoverable workflow at a time.
- Validate the S3 adapter against supported AWS/S3-compatible targets with live fault-injection,
  multipart/large-object strategy, lifecycle reconciliation, and restore drills.

## Release confidence and operability

- CI across supported Python and dependency versions, reproducible lock files, package/Docker smoke
  tests, SBOM generation, dependency scanning, and signed releases.
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

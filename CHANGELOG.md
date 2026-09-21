# Changelog

All notable changes to Algen Agent Runtime will be documented in this file.
The project follows [Semantic Versioning](https://semver.org/) while public
contracts are explicitly marked experimental during the `0.x` series.

## [Unreleased]

_No unreleased changes yet._

---

## [0.1.0a1] - 2026-09-22

First public alpha. Intended for evaluation and contribution; not a
production-readiness claim. Contracts labelled `experimental` may change
without a deprecation window during the `0.x` series.

### Added

- Repository-scoped contributor, extension, agent-building, and connector skills for coding agents.
- A shared Algen ecosystem identity with light/dark Runtime logo and README banner assets, plus
  documented Runtime, Studio, and Marketplace product boundaries.
- Studio-importable Runtime examples shipped with their Python hook providers and configuration in
  source and wheel distributions.
- Runnable cloud chat, typed capability, human checkpoint, governed research, LangGraph adapter,
  multi-agent fan-out, evaluation gate, customer support, incident response, and invoice exception
  examples.
- Runtime DAG manifests for the responsible hiring and teaching-assistant case studies.
- Deterministic configuration, hook-loading, workflow execution, and checkpoint regression coverage
  for the example portfolio.
- Initial open-source governance, security, contribution, and release policies.
- PyPI metadata, deterministic build contents, and clean-install CI checks.
- Public-ready README with an offline quickstart, support matrix, architecture overview, and
  security boundaries.
- API stability and release-process documentation.
- Dedicated PyPI Trusted Publishing, CodeQL, and pull-request dependency-review workflows.
- Separation of the private Algen Agent Studio application from the Runtime distribution.
- Runtime-owned multi-agent workflow manifests and an embedded DAG executor with agent and
  deterministic-handler nodes, conditional execution, clarification limits, bounded validation
  repair, capped dynamic fan-out, correlated child runs, and lifecycle events.
- Application hook registries for workflow payloads, request metadata, structured-output schemas,
  domain validation, and deterministic service execution.
- Runtime predicate and join nodes, declarative input templates, bounded agent loops, deterministic
  ready-node ordering, conditional-branch convergence, host-provided agent execution, and portable
  application hook-provider references.
- Typed, secret-free workflow resource references for documenting node connections to tools, query
  sources, retrieval indexes, memory, caches, stores, telemetry, and application services.
- Runtime-owned JSON Schema Draft 2020-12 workflow input and canonical-output contracts with
  manifest-time schema checks and fail-closed execution validation.
- Tenant-scoped staged artifacts with optional run association, SHA-256 and size metadata,
  scan/available/quarantine states, expiry, metadata listing, deletion, bounded purge, HTTP lifecycle
  endpoints, and a forward PostgreSQL migration.
- Optional PostgreSQL-metadata/S3-blob artifact storage with server-side encryption, service-side
  SHA-256 checks, tenant-hashed keys, conditional creation, compensating cleanup, health checks, and
  an audited compare-and-set `ArtifactScanner` lifecycle service.
- Runtime-owned in-memory and PostgreSQL multi-agent workflow checkpoint stores with tenant isolation,
  optimistic versions, manifest fingerprints, pause/resume without completed-node replay, suspended
  human-wait deadlines, and explicit fail-closed or retry crash recovery.
- First-class Runtime workflow approval nodes with durable approve/modify/reject decisions,
  authenticated reviewer attribution, schema-validated parameter modification, bounded expiry,
  safe rejection propagation, and content-minimal lifecycle events.
- Exact-version parent/child workflow composition with a trusted registry, typed child input and
  canonical output, lineage, cycle/depth guards, nested clarification and approval propagation, and
  checkpoint-first recovery without duplicate child dispatch.
- Provider-neutral, validated email contracts and a TLS-first SMTP adapter exposed as an
  idempotency-aware Runtime tool with artifact-only attachments and content-minimal receipts.
- Shared workflow checkpoint-store conformance coverage plus corruption, ambiguous email delivery,
  and parent/child crash-recovery failure-injection tests.
- Updated the responsible-hiring, customer-support, incident-response, and invoice-exception
  reference workflows to use first-class Runtime approvals and explicit recovery safety instead of
  clarification-shaped action checkpoints.
- DAG-based multi-agent workflow engine (`MultiAgentWorkflowExecutor`) with full topological
  scheduling, fan-out/fan-in, conditional branching, and error recovery.
- Durable execution via in-memory and PostgreSQL checkpoint persistence backends.
- Extended `WorkflowManifest` and contracts with typed node, edge, and dependency schemas.
- OpenAI-compatible model provider adapter and provider contract test suite.
- Approval workflow patterns (`pattern_approval_workflow`, `quickstart_approval`) with
  human-in-the-loop hooks.
- Fixed stale module references and CLI run commands across all example packages.
- Enhanced responsible-hiring case-study UI dashboard.
- Normalize OpenAI and Azure OpenAI response schemas at the provider boundary for strict structured
  outputs: all object fields are required, additional properties are forbidden, and defaults and
  unsupported wire constraints are removed while Runtime retains full result validation.

[Unreleased]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/AlgenAI/algen-agent-runtime/releases/tag/v0.1.0a1

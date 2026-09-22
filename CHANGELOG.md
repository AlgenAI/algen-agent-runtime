# Changelog

All notable changes to Algen Agent Runtime will be documented in this file.
The project follows [Semantic Versioning](https://semver.org/) while public
contracts are explicitly marked experimental during the `0.x` series.

## [Unreleased]

## [0.1.0a2] - 2026-09-22

### Added

- **Provider Instance Identity (WP-01)**: Separated provider deployment identity (YAML mapping key)
  from adapter native type. Supported `registration_id` in `ModelRouter.register_provider()`,
  allowing multiple provider instances of the same adapter type (e.g. multiple Ollama or OpenAI-compatible
  endpoints) with isolated health tracking, rate limiting, token cost accounting, and capability caching.
- **Uniform Capability Narrowing (WP-02)**: Enforced consistent capability narrowing
  ($C_{effective} = C_{adapter} \cap C_{configured}$) across all adapter types. Operators can restrict
  features (such as streaming or tool calling), while configurations attempting to widen unsupported
  capabilities fail-safe to false with diagnostic logging.
- **Provider Reference Validation at Load Time (WP-03)**: Added cross-reference validation in
  `AppSettings` ensuring agent default models, fallback models, and retrieval embedding providers
  reference valid configured provider instances, failing fast with actionable paths during configuration loading.
- **Non-Interactive Automation for Workflow Examples (WP-04)**: Introduced injectable `DecisionProvider`
  abstractions (`ApproveAllDecisionProvider`, `RejectAllDecisionProvider`, `AnswersFileDecisionProvider`,
  and `InteractiveDecisionProvider`) in `examples/workflow_cli.py`. All portable workflow examples now
  support `--approve-all`, `--reject-all`, and `--answers-file PATH` CLI flags, converting `EOFError`
  and non-interactive TTY detection into actionable non-zero diagnostic errors instead of hanging in automation.
- **Project Scaffolding CLI (WP-05)**: Added `algen-agent-runtime new <name>` command supporting
  `--template agent|approval|workflow` to generate production-ready agent projects with `agent.yaml`,
  `app.py`, `hooks.py` (when needed), `test_agent.py`, `README.md`, and `.env.example`. Added `serve`
  subcommand while preserving backward compatibility for invoking `algen-agent-runtime` without subcommands.
- **Safe Starter Tool Pack (WP-06)**: Added an opt-in starter tool pack (`algen_agent_runtime.tools.starter`)
  including `starter.calculator` (AST allowlist without `eval()`, resource and magnitude bounds),
  `starter.json_query` (safe dot/bracket path queries and transforms with byte limits),
  `starter.file_read` and `starter.directory_list` (workspace-rooted with path traversal and symlink-escape
  protection), and `starter.clock` (timezone support and deterministic testing). Starter tools are disabled by
  default in `build_container`, requiring explicit opt-in to prevent expanding default runtime authority.
- **Model Context Protocol (MCP) Client Connector (WP-07)**: Added `algen_agent_runtime.mcp` client integration
  supporting external tool consumption over `stdio` and `sse` transports via the official `mcp` SDK.
  Includes environment filtering for child processes, schema conversion to `ToolDefinition`, namespaced
  tool registration (`mcp.<server>.<tool>`), per-server tool allowlists, timeout and byte limits, and
  secret reference resolution (`env://...`) for remote authentication.
- **Framework Interoperability & Application Registration (WP-08)**: Hardened external framework adapters
  (`LangGraphAdapter`, `OpenAIAgentsAdapter`, `AutoGenAdapter`, `CrewAIAdapter`) with explicit application
  registration via `build_container(framework_adapters=...)` and `container.frameworks.register()`.
  Disallowed arbitrary YAML dotted imports to eliminate code execution vectors. Added an honest governance
  and capability matrix in `docs/framework-adapters.md` distinguishing outer runtime boundaries from
  framework-internal model/tool calls. Added real LangGraph contract and streaming tests.
- **Reproducible Installation & Release Artifacts (WP-09)**: Added SHA256 checksum generation (`SHA256SUMS`)
  for build artifacts. Added clean-environment wheel installation and smoke validation in CI across Python 3.12
  and 3.13 outside repository context, verifying package imports, CLI entrypoint, server title, starter tool exports,
  and scaffolded project test/run execution. Documented reproducible distribution channels (PyPI, GitHub Releases,
  pinned git tags) and step-by-step release rollback and PyPI yanking procedures in `docs/releasing.md`.
- **Documentation Cleanup & Alignment (WP-10)**: Audited documentation tree to ensure no raw assistant
  transcripts, tool execution dumps, or dangling todo counts remain. Added owner, status, and last-reviewed date
  metadata across operational planning documents (`operations.md`, `production-readiness.md`,
  `open-source-pypi-readiness.md`, `api-stability.md`, `why-algen-agent-runtime.md`). Reconciled capability and
  maturity claims across documentation with implemented features (WP-01 through WP-09), and verified that all
  relative documentation links resolve successfully.
- **DNS Rebinding TOCTOU Elimination & Outbound Network Security (WP-11)**: Eliminated DNS rebinding
  Time-of-Check-Time-of-Use (TOCTOU) windows across outbound HTTP requests in `core.http`, `remote_tool`,
  and `HTTPModelService`. Introduced `SafeNetworkBackend` and `SafeAsyncTransport` resolving destination
  hostnames once in worker threads, validating every candidate IPv4/IPv6 address against a comprehensive
  SSRF and cloud metadata deny matrix (private RFC 1918, loopback, link-local, carrier-grade NAT, cloud IMDS,
  unspecified, multicast, and IPv4-mapped IPv6), and pinning TCP socket connections directly to the
  pre-validated IP address. Preserved TLS SNI and server certificate validation by forwarding requested hostnames
  to `start_tls`. Implemented fail-closed multi-address checks, embedded user credential rejection, percent-encoded
  hostname normalization, explicit `allowed_hosts` filtering, and optional `allow_private_networks` override for
  internal VPC test harnesses. Added comprehensive security tests and operational threat modeling documentation.
- **API Admission Rate Limiting & Concurrency Quotas (WP-12)**: Added single-node tenant-aware
  admission rate limiting and in-flight run creation concurrency quotas to protect public API endpoints
  against run, approval, conversation, and artifact floods. Defined `ApiRateLimiter` protocol and
  `InMemoryApiRateLimiter` utilizing a token bucket algorithm for request rate and burst capacity alongside
  in-flight concurrency counters with clock injection for deterministic testing. Added `RouteLimitSettings`
  and `ApiRateLimitSettings` under `api.rate_limiting` supporting per-route overrides (`read`, `write`, `run_create`).
  Integrated FastAPI admission middleware returning HTTP 429 Too Many Requests with `Retry-After` header and
  structured JSON error codes (`RATE_LIMIT_EXCEEDED`, `CONCURRENCY_LIMIT_EXCEEDED`). Exempted health and
  readiness endpoints (`/health/live`, `/health/ready`, `/healthz`, `/ready`) from admission quotas.
- **Hardened Workflow Hook Provider Loading (WP-13)**: Added `WorkflowHookLoader` and `load_hook_provider`
  to securely resolve, validate, and audit dynamic workflow hook providers referenced by YAML manifests.
  Enforces host-configured module allowlists (`allowed_modules`) strictly *before* calling `importlib.import_module()`,
  rejecting unauthorized dynamic imports with `PolicyDeniedError` and preventing untrusted top-level code execution.
  Implements exact segment-based prefix protection preventing prefix confusion attacks (e.g. `examples.test` allows
  `examples.test.child` but rejects `examples.test_bypass`). Validates callable factory shape, compatibility with
  supported API versions (`__api_version__` in `{"1", "1.0", "v1"}`), and `WorkflowHookRegistry` return contracts.
  Generates sanitized audit metadata records (`LoadedHookProvider.audit_metadata`) capturing reference, module, factory,
  package, and package version without serializing code or environment secrets. Updated `examples/workflow_cli.py`
- **Risk-Weighted Storage Integration & Distributed Worker Verification (WP-14)**: Added comprehensive
  unit and integration test suites for Redis and PostgreSQL persistence stores alongside distributed worker
  fencing. Added `tests/unit/test_redis_store.py` verifying `RedisRunStore` and `RedisMemoryStore` tenant isolation,
  key scoping, optimistic concurrency control (CAS conflict detection via Redis transactions), TTL expiration,
  and active run status filtering. Added `tests/unit/test_distributed_workers.py` validating `InMemoryWorkQueue`
  and `DistributedWorker` task claims, lease renewals, worker fencing (stale lease actions fail closed with
  `ConflictError`), bounded retries up to `maximum_attempts`, and graceful cancellation handling. Added
  `tests/integration/test_storage_integration.py` validating PostgreSQL runs, expiring memory, artifacts,
  tool execution deduplication, and workflow checkpoint persistence against both an asyncpg behavioral double
  and live PostgreSQL instances via `POSTGRES_DSN`. Registered `integration`, `postgres`, `redis`, and `live`
  pytest markers in `pyproject.toml`. Added `container-integration` CI job in `.github/workflows/quality.yml`
  running PostgreSQL 16 and Redis 7 service containers. Documented Studio and Runtime alignment requirements
  in `docs/studio-runtime-alignment.md`.

### Fixed

- Fixed silent collision and overwrite when multiple providers of the same type were configured.
- Prevented capability cache collisions across different instances of the same provider type.

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

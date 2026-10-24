# Changelog

All notable changes to Algen Agent Runtime will be documented in this file.
The project follows [Semantic Versioning](https://semver.org/) while public
contracts are explicitly marked experimental during the `0.x` series.

## [Unreleased]

### Added

- OAuth refresh-token Gmail adapter (`communications.gmail`) supporting governed outbound delivery,
  bounded inbox metadata reads and on-demand attachment downloads without placing mailbox secrets in
  manifests or model context. See `docs/email.md`.
- Typed TypeSafe Jev System One client for confidence-aware Choice, Score, and Noul decisions.
- `DocxDocumentExtractor`: dependency-free DOCX inspection with hidden-run, contrast-aware
  low-visibility, macro, remote-template and zip-bomb checks; `extractor_for` accepts `.docx`. See
  `docs/document-security.md`.
- `ContinuationPolicy` on agents: a run that reaches its step, token or cost budget can continue
  with small automatic extensions and then pause for approval instead of failing, resuming from its
  checkpoint (`run.continued` event). Time spent paused no longer counts against the run deadline.
- `DELETE /v1/conversations/{id}` removes a conversation with its messages, events and feedback.

### Changed

- The Anthropic adapter supports native structured output: `response_schema` is sent as
  `output_config.format`, profile `output_config` extensions are merged with it, parallel tool
  results share one user turn, and `stop_reason` maps to `finish_reason` (`length`,
  `content_filter`). The adapter moved to `models.providers.anthropic` and is still importable from
  `models.providers.adapters`.
- `classify_findings` is shared by all document extractors.

### Fixed

- OpenAI-compatible streaming now reports token usage (#28). Streamed calls send
  `stream_options: {include_usage: true}` and read usage from the final chunk, whose `choices` list
  is empty, so budgets, cost estimates and host metering count streamed calls. Servers that reject
  the field set the provider option `stream_usage: false`; Mistral leaves it off by default.
- Declining a budget-continuation approval ends the run even when the agent's
  `ApprovalPolicy.on_rejection` is `continue`, instead of reporting a phantom declined tool call.

## [0.1.0a8] - 2026-10-22

### Added

- Declarative HTTP API tools (`AppSettings.http_tools`): method, URL template, query, header and
  body templates, JSON Schema parameters, `none`/`bearer`/`basic`/`header` auth by `env://`
  reference, `link_header`/`page_param` pagination, JSON-pointer response selection, timeouts and
  size limits. Calls use the SSRF-safe client without redirects; settings validation requires each
  tool's host in `security.allowed_http_hosts`. See `docs/http-tools.md`.
- Workflow tool steps: a `handler` node with `handler: tool:<name>` and `arguments` calls a
  registered tool and stores its result.
- `ToolPolicy` on agents: `on_error="return_to_model"` reports a failed tool call to the model as
  its result instead of failing the run; `parallel_calls` runs one reply's tool calls concurrently
  (up to `max_parallel_calls`) when none needs approval.
- `ApprovalPolicy.on_rejection="continue"`: a rejected approval tells the model the action was
  declined (with an optional reason) and the run continues.
- `GuardrailPolicy.tool_policies` selects the policies applied to tool arguments and results;
  `GuardrailPolicy.tool_result_injection` (`flag` or `block`) screens tool output that tries to
  instruct the model.
- `MemoryPolicy.store_tool_payloads` (previously unused) now keeps a turn's tool calls and results
  in session memory, capped by the new `tool_payload_max_chars`. See `docs/agent-tooling.md`.

### Changed

- The tool executor passes the agent to the policy engine, so an agent's
  `guardrail_policy.policies` apply at `before_tool` and `after_tool` as at other boundaries
  (previously every registered policy ran there). Agents with the default list are unaffected.
- Session history read from memory starts at a whole turn and never holds a tool call without its
  result.

## [0.1.0a7] - 2026-10-19

### Added

- Context evaluation cases check exact message retention, required/forbidden text, message-token
  and checkpoint-size limits, complete tool exchanges, and ordered archive recovery. Content-free
  reports reject missing evidence and failed cases; an offline regression example covers simple,
  compacted, archive-backed, and structured-state/skill contexts. Context events now expose token
  breakdowns, compaction reductions, and typed planning/skip reasons without message content.

- Context compaction adapts its trigger and summary input to configured builder/model allowances,
  with output, tool/schema, safety, retrieval, and protected-context accounting. A nullable trigger
  ratio retains fixed-threshold behavior when needed. Opt-in `archive_compacted_history` reduces
  checkpoint payloads by retaining an absolute history offset, protected messages, summary, and tail;
  older sources use governed archive chains and artifact retention. Legacy inline checkpoints load
  with default fields. New opaque context artifact IDs avoid accidental PII redaction of recovery
  links; existing UUID artifacts remain readable and reusable.

- Opt-in learned memory stores facts, preferences, and procedures across runs with tenant/user/agent
  isolation, runtime provenance, bounded explicit recall, expiry, correction, and deletion. Governed
  `core.memory_recall`, `core.memory_write`, and `core.memory_delete` tools enforce permissions and
  approval policies. PostgreSQL and ephemeral adapters use revision checks and atomic operation
  receipts so retries cannot resurrect deleted or corrected content. Maintenance clears expired
  payloads while retaining replay metadata.

- Opt-in progressive skill loading exposes bounded descriptions from `AgentDefinition.skills` and
  loads up to four permitted instruction bodies through `core.skills_load`. Explicit selections
  replace or clear the active guidance. Policies, source hashes, and run/tenant/user checks govern
  loads and replays; checkpoints retain accepted guidance independently of history compaction.
  Protected context blocks and separate catalog/instruction budgets bound model input.

- Opt-in structured working state retains goals, constraints, decisions, completed work, and open
  tasks independently of history summaries. The governed `core.working_state_update` tool uses
  revision checks and bounded full replacements; run checkpoints preserve accepted records and
  replay markers together. Built-in builders and model-budget packing protect the current record
  as untrusted assistant notes. Content-free revision events follow checkpoint persistence.

- Built-in builders retain canonical execution history separately from packed model messages.
  Opt-in history compaction summarizes bounded, complete message groups, preserves recent context,
  and archives source messages with recovery links through `core.context_read`. Summaries remain
  untrusted historical data. Model policies, allowlists, token/cost budgets, and deadlines apply;
  usage survives validation/storage failures. Completed compactions persist their cursor before
  publishing the content-free `context.compacted` event.
- Opt-in `AgentDefinition.context_policy` offloads oversized processed tool results to the configured
  artifact store. Bounded previews and the governed `core.context_read` paging tool keep payloads
  recoverable without sending everything to the model. Reads and completed-read replays enforce
  tenant/run/user boundaries, expiry, lifecycle state, and content identity. Offloaded references
  persist in run checkpoints and appear in `RunResult.artifacts`; audit records exclude content.
- Optional `Tool.replay_validator` lets lifecycle-sensitive tools validate access before returning
  a completed execution or cached result. Existing tools preserve their replay behavior by default.
- Optional `ModelProfile.context_window_tokens` and `context_safety_margin_tokens` (default 256)
  enable per-candidate estimated context budgeting. Generate and streaming preflight reserve output,
  tool definitions, response schemas, extensions, and safety headroom before packing messages.
  Each fallback uses the original request, without mutating run state. Candidates that cannot fit
  are skipped without affecting provider health; all-over-budget candidates raise `BudgetExceededError`.

### Fixed

- Automated releases preserve changelog Markdown as literal release notes and target the triggering
  commit. Release packaging verifies context modules and migration 011, then runs all six offline
  context examples from the installed wheel outside the checkout.
- Context-building timeouts now transition to `timed_out`, including timeouts during summarization.
- Context builders no longer duplicate session history on repeated tool-loop builds. Disabled memory
  and a zero history limit prevent loading prior session messages.
- Context packing preserves system instructions, the latest user message, and the newest message
  group; tool calls and their results are retained or evicted together. Estimates include tool
  arguments and content metadata. Required context that exceeds the configured estimate limit now
  raises `BudgetExceededError`; orphan tool results raise `ConflictError`.
- Retrieval enforces context limits even with no results, skips oversized evidence, and reports
  citations only for evidence that fits in the packed context.

### Upgrade notes

- Migration `011_learned_memory.sql` adds learned records and replay receipts, independent of source
  run retention. Enable `learned_memory_policy`, selected memory tools, and their permissions.
  `storage.learned_memory_store` defaults to ephemeral `memory`; select `postgres` for durability.
  Strict agent-definition readers must support the new defaulted policy. Content-free revisions and
  receipts are retained without automatic garbage collection. See `docs/learned-memory.md`.

- Agent manifests gain default-empty `skills`, and run checkpoints gain default-empty `loaded_skills`.
  Upgrade strict readers/workers together; no SQL migration is needed. `core.skills_load` is reserved
  and requires explicit enablement, `skills.read`, and `context_policy.skills_enabled`. Definitions
  must match when resuming a run; changed or revoked loaded skills fail closed. See `docs/skills.md`.

- Checkpoints gain optional `working_state`; upgrade strict readers and workers together without a
  SQL migration. The reserved `core.working_state_update` tool requires `working_state_enabled`,
  explicit enablement, and `context.write`. Existing write-approval rules apply. Working state is
  run-local and disabled by default; custom builders cannot enable it. See `docs/context-management.md`.

- Run checkpoint JSON now includes defaulted `context_history` and `context_compaction` fields.
  Upgrade all readers/workers together; no SQL migration is required. Legacy checkpoints can seed
  canonical history only from surviving active messages. Extensions should append execution messages
  through `RunState.append_message()` instead of mutating only the active view. Canonical history
  remains inline by default. Opt-in archive-backed checkpoints add defaulted history offset and
  pinned-message fields; archived source recovery follows artifact retention rather than run retention.
- Tool-result offloading is disabled by default. Enable `core.context_read` and grant `context.read`
  before enabling it. The default container registers this reserved tool and uses its configured
  artifact store; direct runtime construction must supply `context_artifacts`. Run checkpoint JSON
  gains a default-empty `context_artifact_ids` field, without a database migration. All workers
  reading these new checkpoints must run the updated Runtime because older strict readers reject
  unknown fields. Ephemeral artifact storage does not survive process restarts.
- Model context budgets are opt-in and require an explicit positive output cap for direct router
  calls. Configured budgets currently reject image/file inputs and extensions that override normalized
  request fields. Existing profiles without a context window retain their previous routing behavior.
- Oversized required inputs and tool exchanges previously could bypass context limits. They now
  fail explicitly: reduce payloads or increase the builder's configured limit. The serialized-message
  estimate can evict older history sooner than the previous text-only estimate. These estimates are
  not provider-specific token counts; no persisted schema or optional dependency changes are needed.

### Known limitations

- Context estimates are provider-neutral text estimates, not exact tokenizer counts or multimodal
  capacity measurements. Offline evaluation gates do not establish real-model summary accuracy.
- Memory-backed artifacts and learned records are ephemeral. Expired, deleted, or quarantined
  archives cannot be recovered through the runtime, even when their summary survives.
- Learned memory uses explicit literal recall; it does not automatically extract or semantically
  retrieve facts. Content-free replay receipts and revision tombstones have no automatic cleanup.
- Protected messages, uncompacted tails, and artifact references can still grow. Archive-backed
  checkpoints reduce payload duplication but do not guarantee a fixed checkpoint size.

## [0.1.0a6] - 2026-10-12

### Upgrade notes

- Deployments using `approval_store: postgres` should upgrade: approval decisions failed in earlier releases (see Fixed).
- Migration `010_retention_indexes.sql` runs on startup and creates indexes without `CONCURRENTLY`, briefly locking writes on the runs, approvals, tool-execution, memory, and audit tables. On large tables, create the indexes concurrently beforehand (see `docs/operations.md`).
- Maintenance is opt-in: set `maintenance.enabled: true` or schedule `algen-agent-runtime maintenance`. No records are deleted unless a retention window is configured.

### Added

- **Maintenance sweeps and data retention**: `maintenance` settings and an `algen-agent-runtime maintenance` command, plus an optional background loop in the API process. Each sweep expires paused runs whose approval request has expired (and, if configured, clarifications that waited too long), purges expired memory and artifacts, and, when opted in, deletes terminal runs with their events, approvals, and tool-execution records, and old audit events. Runs with unresolved side effects or an unfinished parent workflow are kept. Sweeps are batched and use `SKIP LOCKED`, so replicas can run them concurrently. Record deletion is off by default.
- **Retention indexes**: Migration `010_retention_indexes.sql` adds indexes for run age, run-scoped approvals and tool executions, memory expiry, and audit age.

### Fixed

- **PostgreSQL approval decisions**: `ApprovalRequest.expires_at` is now typed as a `datetime`. It previously came back from the JSON column as a string, so every approve, reject, or modify decision with `approval_store: postgres` failed with a `TypeError`.
- **Redis run-store conflicts**: A concurrent write between `WATCH` and `EXEC` now raises `ConflictError` instead of leaking redis-py's `WatchError`.
- **Direct planner tool calls**: The `direct` planner now executes tool calls the model requested instead of calling the model again with unanswered calls. The runtime also refuses to invoke a model while tool calls are unresolved, so a planner bug fails the run instead of sending providers an invalid transcript.
- **Enum values in redaction**: Secret and PII redaction no longer turn `StrEnum` fields such as `Message.role` into plain strings.
- **OpenAI `gpt-6` models**: Use `max_completion_tokens`, as for `gpt-5` and o-series models.
- **Starter tools on minimal hosts**: The clock tool resolves UTC without a system time-zone database, `tzdata` is installed on Windows, and directory listings return POSIX-style relative paths on every OS.

### Changed

- **Live Redis integration tests**: The container integration job now exercises the Redis run, memory, and cache stores against the live Redis 7 service instead of only an in-memory double.

## [0.1.0a5] - 2026-10-12

### Changed

- **Agent Manifest UI visual refresh**: Aligned the built-in manifest explorer with the Runtime documentation's warm gold, parchment, and charcoal design system. The UI now uses a responsive manifest sidebar, refined cards and DAG canvas, persistent light/dark theme preference, and a compact validation status bar while remaining fully self-contained.
- **Runtime brand palette alignment**: Recolored the existing Runtime logos and banners to the warm gold, ivory, and charcoal Algen palette used by `algen.ai` and Agent Studio, without changing their geometry, typography, wording, or composition.
- **Production-readiness status audit**: Reconciled the roadmap with current implementation and CI evidence, separating completed single-node, persistence, readiness, distributed-worker, and release controls from the remaining production gates. Corrected the previous claim that Redis was exercised against the live CI container; the current Redis tests use a behavioral double.

### Fixed

- **Preserve PostgreSQL run-request object order**: Store serialized run state as PostgreSQL `json` instead of `jsonb`, with a forward migration for existing installations, so order-sensitive JSON Schema properties and request metadata reach model providers in caller-defined order across create and save cycles.

## [0.1.0a4] - 2026-09-24

### Added

- **Agent Manifest UI (Swagger-Style Visualizer)**:
  - Interactive browser UI and DAG canvas for inspecting, visualizing, and testing `agent.yaml` specifications and multi-agent workflows.
  - Generates self-contained static HTML via `algen-agent-runtime ui` and `export-ui` CLI commands with zero external dependencies and zero CDN reliance.
  - Built-in runtime server endpoint at `/agent-ui` (or `/ui`) and structured JSON manifest endpoint at `/agent-ui/spec` via `api.agent_ui_enabled: true` or `algen-agent-runtime serve --ui`.
  - Comprehensive visualization tabs for agent catalog, DAG workflow topology, provider model matrix, tools/MCP servers, retrieval stores, and live test run playground.
- **Container Lifecycle Management for Framework Adapters**:
  - Registered `framework_adapters` are now managed as container lifecycle resources, automatically invoking `start()` and `aclose()` during `container.astart()` and `container.aclose()`.
- **Workflow Hook Provider Teardown Protocol**:
  - Added async `aclose()` lifecycle hook to `LoadedHookProvider` and `WorkflowHookLoader`, cleanly closing factory-managed resources upon shutdown.
- **Workflow Manifest Delimiter Validation**:
  - Validated template placeholder syntax during manifest loading, rejecting unmatched `{{` or `}}` placeholder delimiters.
- **Contribution Guidelines & DCO Signoff Policy**:
  - Added root `AGENTS.md` and updated `CONTRIBUTING.md` enforcing DCO signoff (`git commit -s`), optional dependency test isolation (`pytest.importorskip` / `sys.modules` masking), PostgreSQL/Redis persistence constraints, and automated Gitleaks secret scanning.

### Removed

- **Clean Removal of Obsolete Rate Limiting Alias (A3-04)**:
  - Removed deprecated `max_concurrent_runs_per_tenant` configuration alias, limiter argument, and property in favor of `max_concurrent_run_requests_per_tenant`. Configurations specifying the obsolete alias are strictly rejected with `ValidationError`.

## [0.1.0a3] - 2026-09-24

### Fixed

- **Preserve Unspecified Capabilities in Partial Narrowing (A3-01)**: Fixed `_narrow_capabilities()` in `ModelRouter` to check `override.model_fields_set` before evaluating effective capabilities. Omitted fields in partial `capabilities` configuration blocks now inherit adapter-reported capabilities rather than erroneously defaulting to `false`. Empty overrides (`{}`) preserve all adapter capabilities, and partial override policies remain isolated in capability cache keys.
- **Repair Shipped Example Configurations and Enable Automatic Discovery (A3-02)**: Repaired provider references in `examples/pattern_provider_fallback/agent.yaml` (`primary`, `fallback`, `local`) and `examples/quickstart_local_chat/agent.yaml` (`local`, `local-private`, allowlist `local-private/qwen3:8b`) to use configured provider instance keys rather than adapter type names. Replaced manual configuration lists in `tests/unit/test_example_workflows.py` with automatic discovery across all shipped `agent.yaml` files, guaranteeing that every example configuration parses and validates against `AppSettings`.
- **Enforce Streamed Request-Body Wire Limits (A3-03)**: Added pure ASGI `RequestBodyLimitMiddleware` to validate and enforce request body limits before and during streaming body reads. Rejects invalid non-integer or negative `Content-Length` headers with HTTP 400 (`INVALID_CONTENT_LENGTH`), rejects declared lengths exceeding limits with HTTP 413 (`REQUEST_PAYLOAD_TOO_LARGE`) prior to body consumption, and enforces limits on actual received wire bytes regardless of missing or misleading headers (including chunked requests). Returned error payloads contain stable error codes and limit byte fields without echoing request contents.
- **Deterministic Customer-Support Example Automation (A3-05)**: Added committed synthetic answers fixture (`examples/reference_customer_support/fixtures/automation-answers.json`) providing ordered clarification and approval answers for non-interactive execution. Updated customer-support README documentation and `tests/integration/test_portable_examples_cli.py` to use `--answers-file` for guaranteed zero-input automation. Improved `ApproveAllDecisionProvider` and `RejectAllDecisionProvider` clarification error messages to provide valid command syntax patterns.
- **Environment-Independent Optional Dependency Testing (A3-06)**: Made optional dependency tests independent of the developer environment by isolating package imports with `monkeypatch.setitem(sys.modules, ...)` instead of assuming missing packages. Added forced-missing tests for `OpenAIAgentsAdapter` and `MCPClientManager` (stdio and SSE transports), introduced an `optional-integration` CI workflow job testing `[dev,frameworks,mcp,traccia]`, and documented framework adapter evidence and maturity levels.
- **Scaffolded Project Correctness & Unresolved Template Rejection (A3-09)**: Enforced strict validation and rejection of unresolved placeholders in multi-agent workflow `input_template`. Added static DAG validation in `WorkflowManifest` rejecting unsupported nested syntax (e.g. `{{step_one_data.input}}`) and undeclared keys with errors naming the node and placeholder. Enhanced `MultiAgentWorkflowExecutor._render_template()` with runtime guards raising typed `ConfigurationError` if any unresolved placeholder remains. Fixed the `workflow` template in `algen-agent-runtime new` to use valid `{{step_one_data}}` syntax, strengthened generated test assertions to verify rendered values and reject placeholder leakage, and enabled `api.rate_limiting.enabled: true` in all scaffolded configurations.



### Added

- **Functional MCP Integration with Transports, Governance, and Safe Egress (A3-10)**:
  - Added declarative `mcp_servers` configuration in `AppSettings` supporting `stdio`, `streamable_http`, and legacy `sse` transports with unique name enforcement.
  - Built on official `mcp>=1.30.0,<2` SDK using `streamablehttp_client` and `stdio_client`.
  - Hardened remote MCP transports via `_make_http_client_factory` backed by `create_safe_http_client` and `SafeAsyncTransport` with DNS pinning, host allowlists, and private IP blocking.
  - Implemented secret hygiene rejecting literal sensitive headers (`Authorization`, `X-API-Key`, `Cookie`, etc.) in configuration, requiring `env://` secret references for `auth_token_ref` and `secret_headers`.
  - Added governance mapping precedence: operator `tool_policies` override > trusted server annotations (`trust_tool_annotations: true`) > secure defaults (`SideEffect.EXTERNAL`, `Idempotency.NON_IDEMPOTENT`).
  - Handled tool discovery with cursor pagination, container lifecycle integration with graceful degraded dependencies for optional servers and clean shutdown cleanup, and cancellation-safe execution.
- **Promote OpenAI Agents SDK to Supported Adapter Surface (A3-11)**:
  - Supported real, credential-free execution using official `agents.testing.ScriptedModel`.
  - Added comprehensive real SDK integration contract tests covering invoke, two-turn function tool loops, streaming event normalization (`STARTED`, `DELTA`/`STEP`, `COMPLETED`), cancellation safety, and runtime metadata propagation (`run_id`, `tenant_id`, `user_id`, `session_id`).
  - Strengthened `_BaseAdapter.cancel` to await cancelled task completion and reliably clear active task tracking.
  - Forwarded `RunConfig(tracing_disabled=True)` by default to isolate testing from external telemetry servers.
  - Aligned adapter capabilities and documentation to honestly reflect unsupported durable resume (`persistence=False`), while surfacing interruptions as `AWAITING_INPUT` with bounded serializable metadata.
  - Added runnable offline pattern example `examples/pattern_openai_agents_integration` demonstrating application registration and container framework registry execution.

### Changed

- **Run-Creation Request Concurrency Naming (A3-04)**: Clarified API rate limiting terminology with `max_concurrent_run_requests_per_tenant`. The quota bounds in-flight HTTP request concurrency during `POST /v1/runs` rather than active executing run lifetimes. Removed the misleading pre-release configuration alias, limiter constructor argument, and property instead of carrying compatibility shims into the public surface.
- **Corrected Security, Interoperability, and Maturity Claims (A3-07)**:
  - Updated `docs/mcp.md`, `docs/threat-model.md`, and `docs/production-readiness.md` to explicitly document MCP stdio execution as unsandboxed operator code running on the host system without process isolation, while clarifying that remote transports enforce DNS-pinned safe egress and header secret references.
  - Documented conservative server annotation trust model (`trust_tool_annotations: false` by default).
  - Clarified framework adapter boundaries in `docs/framework-adapters.md`, `docs/why-algen-agent-runtime.md`, and `README.md`, emphasizing that `AgentRuntime` does not automatically dispatch configured agents through foreign adapters and that foreign adapters are made available to application code via explicit trusted registration.

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

[Unreleased]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a7...HEAD
[0.1.0a7]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a6...v0.1.0a7
[0.1.0a6]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a5...v0.1.0a6
[0.1.0a5]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a4...v0.1.0a5
[0.1.0a4]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a3...v0.1.0a4
[0.1.0a3]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a2...v0.1.0a3
[0.1.0a2]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a1...v0.1.0a2
[0.1.0a1]: https://github.com/AlgenAI/algen-agent-runtime/releases/tag/v0.1.0a1

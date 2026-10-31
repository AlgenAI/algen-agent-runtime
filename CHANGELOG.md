# Changelog

All notable changes to Algen Agent Runtime will be documented in this file.
The project follows [Semantic Versioning](https://semver.org/) while public
contracts are explicitly marked experimental during the `0.x` series.

## [Unreleased]

### Fixed

- A toolkit whose client cannot open because of its configuration (a missing setting or
  credential, a missing extra) raises `ToolkitConfigurationError`, a `ConfigurationError` and a
  `ToolNotAttemptedError`, with the toolkit's message (what to set or install). In 0.1.0a19 it
  raised a bare `ToolNotAttemptedError` naming only the error type, so callers catching
  `ConfigurationError` missed it and the actionable message was lost. Other client errors still
  keep only their type, since their text may quote what they were given.

## [0.1.0a19] - 2026-10-31

No database migrations. 0.1.0a18 and 0.1.0a19 can run side by side during a rolling upgrade (see
the last upgrade note).

### Upgrade notes

- **Breaking (governance fix):** `ToolExecutor.execute` now enforces `requires_approval` (an MCP
  overlay's `approval: true`, `@tool(..., approval=True)`). A workflow `tool:` step on such a tool
  runs only after an upstream `approval` node was approved or modified in the same run, and a
  workflow with such a step and no approval node before it cannot start. Code that calls the
  executor directly must pass `approval=` (a reference to the approval that covers the call).
  Before, these calls ran without approval. Agent runs are unaffected. (#83)
- DOCX and PPTX documents with both text and images now report `document.images_excluded`
  (`low`), as PDF does, so their disposition is `review`, not `pass`. Pipelines that accept only
  `pass` route these documents to review. (#81)
- `tool_result_injection` now catches instructions at the start of a line, so `flag` and `block`
  act on tool results they let through before. (#83)
- `budget.max_cost_usd: 0` now lets zero-cost models run (any spend still stops the run); it no
  longer stops every run. (#83)
- A toolkit whose client fails to open at start degrades the service instead of stopping it; pass
  `Toolkit(..., required=True)` to keep failing fast. (#83)
- A workflow agent node's invalid JSON output is repaired up to `max_repairs` (more model calls)
  and, once repairs run out, fails with `VerificationError` instead of `ConfigurationError`. (#83)
- The `pii` guardrail leaves single addresses in tool result fields declared `format: email`
  unredacted. (#83)
- New fields are left out of stored state while empty or unset (run requests, workflow
  checkpoints, manifests), so 0.1.0a18 replicas can still read what this release stores and
  workflow manifest fingerprints do not change. Send attachments only once every process runs
  this release.

### Added

- File attachments for runs, conversations and workflows (vision and PDF input). Callers pass
  uploaded artifacts by reference (`attachments: [{artifact_id, media_type?}]`) on `RunRequest`,
  `POST /v1/runs`, `POST /v1/conversations/{id}/messages` and `POST /v1/workflows/{name}/runs`,
  and `algen-agent-runtime run AGENT INPUT --attach FILE`. Agents opt in with
  `AgentDefinition.attachments` (`enabled`, `types`, `max_count`, `retention`), workflows with
  `WorkflowManifest.attachments` and `WorkflowNode.pass_attachments`; the deployment bounds them
  with `security.allowed_attachment_types`, `max_attachments_per_run` and
  `max_attachment_bytes_total`. Before any model call a run is refused if the agent did not opt
  in, a file is missing, another tenant's or not available, its real type (read from the bytes)
  is not allowed, a limit is exceeded, or no configured model can read it; an `attachments`
  policy point lets hosts veto or filter files. History, memory, checkpoints, events and spans
  hold only references; the bytes are loaded onto a copy of the request just before each provider
  call. `retention: turn` (default) replaces the file with a note in memory after the turn.
  Models without the capability are skipped, fallbacks included. Anthropic sends image and
  document blocks (and URL images) and reports `files`; OpenAI-compatible providers send data-URL
  images and, where supported, PDF file parts. `ToolContext.attachments` and
  `ToolContext.read_attachment()` let tools read the run's files. `BasicArtifactScanner` and
  `security.upload_scanner: basic` scan uploads as they arrive. `GoldenQuestion.attachments` for
  evaluation. See docs/attachments.md. (#82)
- `ToolContext.session_id` (the run's `session_id`), also declarable in
  `ToolDefinition.cache_context`. (#79)
- `Toolkit(..., required=True)`. (#83)

### Fixed

- A tool-result cache with `scope: session` no longer fails every cacheable tool call: the
  session reaches the cache key, and a scope the call cannot fill skips the cache instead of
  failing the run. (#79)
- `ArtifactLifecycleService.scan` records the `artifact.scan` evidence before it changes the
  artifact's status, so no artifact becomes available without scan evidence; a failed audit
  write leaves it `pending_scan` for a retry. A scan that loses a concurrent transition is
  followed by `artifact.scan.discarded`. (#80)
- `tool_result_injection` screens the decoded strings of a tool result, not its JSON encoding,
  which hid line-start instructions behind `\n`. (#83)
- `requires_approval` is enforced by the tool executor, not only when planning. (#83)
- A plugin can register a mandatory policy: `context.policies.register(policy, mandatory=True)`
  raised `TypeError`. (#83)
- Workflow `tool:` steps work under `algen-agent-runtime serve`: `AlgenAgentRuntimeClient`
  exposes `tools` and `tool_executor`. (#83)
- A `PolicyDeniedError` raised by a tool is recorded as not attempted (`failed`), not
  `indeterminate`, so a refused email is not reported as possibly sent and can be retried. (#83)
- The `pii` guardrail no longer redacts sender and recipient addresses from mail tools' declared
  address fields, so an agent can reply to what it read. (#83)
- Ollama: private reasoning that some models put in `content` (ending in `</think>`) is stripped
  from replies, streamed or not. (#83)
- A workflow agent node's unparseable JSON goes through the repair loop. (#83)
- `budget.max_cost_usd: 0` no longer stops runs on free models. (#83)
- A toolkit that cannot start no longer stops the service from booting. (#83)

### Deprecated

- The document extractors in `algen_agent_runtime.security` (`documents`, `docx`,
  `docx_contrast`, `pptx`, `xlsx`, `html`, `ooxml`) raise `DeprecationWarning` on import. They
  move to `algen-agent-tools` (`algen_agent_tools.documents`, AlgenAI/algen-agent-tools#36) and
  keep working here for at least one more release. (#81)

## [0.1.0a18] - 2026-10-30

### Added

- An approval inbox. `GET /v1/approvals` lists the tenant's approval requests, newest first,
  filtered by `status`, `run_id` and `agent`, and paged with `limit` (1 to 500) and `offset` (0 to
  1,000,000); `GET /v1/approvals/{approval_id}` reads one. `ApprovalService.list(...)` does the
  same in Python, on the in-memory and PostgreSQL stores. A pending request past its expiry is
  reported as `expired` (`ApprovalRequest.effective(now)`) without changing the stored request.
  Both routes need the new `approvals:read` scope: the inbox shows every user's pending actions in
  the tenant, conversation turns included, so grant it to approvers on purpose. `runs:read` is not
  enough. `modified_parameters` are returned with the same redaction as `redacted_parameters`.
  See docs/operations.md#approval-inbox. (#61)
- `ApprovalRequest.agent` and `ApprovalRequest.created_at`. Migration `015_approval_agent.sql` adds
  an `agent` column and an index on `(tenant_id, agent, created_at DESC)` to
  `algen_agent_runtime_approvals`; PostgreSQL keeps both fields in columns, and `created_at` is the
  row's time. Requests stored by earlier releases have no `agent`.
- `storage.postgres_schema` (and `PostgresDatabase(..., schema=...)`): an optional PostgreSQL
  schema for the Runtime's tables, so they can share a database with an application's own. Every
  pooled connection starts with `search_path` set to that schema alone, so migrations and all
  PostgreSQL stores use it; with `initialize_schema` it is created when missing. Start-up fails if
  the schema is missing (without `initialize_schema`), not usable by the role, or not in effect,
  for example behind a pooler that drops the startup parameter. If the schema is empty while
  `public` still holds the Runtime's tables, start-up logs
  `postgres_schema_is_empty_but_public_has_runtime_tables`. Unset by default: no change for
  existing deployments. See docs/operations.md#sharing-a-database-postgres_schema. (#63)
- `MarkdownChunker` and `retrieval.<name>.chunker: markdown` (default `text`, unchanged). It
  splits Markdown at its headings, never inside fenced code; keeps paragraphs, lists, tables and
  code blocks whole where they fit (a split code block is re-fenced, a split table repeats its
  header); and records `heading_path`, `heading`, `heading_level` and a unique GitHub-style
  `anchor` in each chunk's metadata. A chunk's `uri` becomes `<document uri>#<anchor>`, so
  citations link to the section. Parsing is linear in the document's length. `Chunker`, the
  protocol both chunkers satisfy, types the `chunker=` argument of the in-memory retriever and the
  vector stores. See docs/rag.md#markdown-chunking. (#59)
- `http_tools[].auth.type: caller`: a declared HTTP tool sends the calling user's credential for
  the run (`ToolContext.credentials[credential]`, default `authorization`) as `header_name`
  (default `Authorization`), with an optional `scheme` prefix. A missing or blank credential, or
  one that is only the scheme, fails with `authentication required` before any request; a value
  carrying another scheme, or a control character anywhere, is refused. The credential goes only
  to the declared origin: link-header pagination to another origin fails the call. Cookie, Host,
  Proxy-Authorization, hop-by-hop and framing headers are refused as `header_name`, and
  `headers` may not set the auth header. See docs/http-tools.md#calling-the-api-as-the-signed-in-user
  and docs/threat-model.md. (#64)
- `ToolDefinition.cache_context` and `build_tool(..., cache_context=...)` (also `@tool` and
  `Toolkit.tool`): a tool names the `ToolContext` values its result depends on (`tenant_id`,
  `user_id`, `run_id`, `step_id`). The tool-result cache adds them, digested, to the key, so a tool
  bound to the caller's user, run or step is never served another caller's result. Tools that
  declare nothing keep their cache keys. The component inventory reports the field. (#70)
- `ToolNotAttemptedError`, a `ToolExecutionError` for a call a tool refuses before contacting
  anything. The ledger records it `failed`, not `indeterminate`, even for writes.
- Provider types `gemini` (Google Gemini API), `xai` (Grok) and `groq`, built on the
  OpenAI-compatible adapter with vendor base URLs and default `env://GEMINI_API_KEY`,
  `env://XAI_API_KEY` and `env://GROQ_API_KEY` secrets. `base_url` and `capabilities` can be set per
  provider entry. Gemini supports embeddings (`gemini-embedding-001` by default) and image input.
  Groq reports strict `json_schema` support only for gpt-oss models; other models get
  `strict: false`. Configured `capabilities` replace these defaults.
- `routing_preference: ordered` on model profiles: candidates are tried in declaration order
  (default model, then `fallback_models`), while circuit-breaker, capability, latency and cost
  filters still apply. It applies to the agent's whole model list: configuration that sets it on
  only some profiles is rejected, and in code the default profile decides.
- `ProviderError.status_code`, the provider's HTTP status when there is one.
- `telemetry.otlp_metrics_endpoint` and `telemetry.otlp_metrics_export_interval_seconds`
  (default 60): export the Runtime's OpenTelemetry metrics to an OTLP/HTTP collector through a
  periodic reader. If an SDK meter provider is already installed (by Traccia or the application),
  it is kept and the setting is ignored with a warning. The API flushes the provider on shutdown.
  See docs/operations.md#traccia.
- `AgentRuntime.steer(run_id, tenant_id, text)` and `POST /v1/runs/{run_id}/steer` (`runs:write`;
  for a conversation turn's run, only the conversation's owner) queue a user message for a run that
  has not ended. It is delivered before the next model call, after the current tool results, with
  a `steer.delivered` event, and is checkpointed in `RunState.pending_steering`, so it survives a
  pause. Messages still waiting when the run ends are `RunResult.undelivered_steering`, with a
  `steer.undelivered` event. At most 16 messages of 8,000 characters each. Conversation turns do
  not expose steering yet. See docs/operations.md. (#71)
- `ToolContext.emit_note(title=, paths=, suggestions=, data=)` writes a bounded `tool.note` run
  event for the call (step and call id) that never reaches the model. (#71)
- A tool can report a failure it returns as data: `ToolResult(value=..., failed=True)` from the
  handler, or `ToolDefinition.failure_field` / `@tool(failure_field="ok")` (a result whose field is
  `false`). The call is recorded as failed in `tool.completed`, the audit record and the span; the
  payload still goes to the model whatever `on_error` is; it is not retried or cached, and it does
  not reset refusal counts or feed the repeat guard. The ledger records it `completed` with
  `result.failed`, so a replay returns it without running the call again. (#71)
- `Tool.with_handler(handler)` returns a copy of the tool with a new handler, keeping its class,
  definition and every hook. `tools.contracts.wrap(tool, middleware)` is shorthand for
  `tool.with_handler(middleware(tool.handler))`. (#71)
- `ModelRouter(reserve_input_margin=...)` and `build_container(usage_meter_margin=...)` (default
  1.0, at least 1.0) scale the input tokens reserved with a `UsageMeter`, rounding up; `4/3`
  reserves as if a token were three characters. Context packing and the token budget are
  unchanged. (#71)
- Persistence drills (`tests/drills`, pytest marker `drill`, CI job `persistence-drills`): real
  Runtime processes on live PostgreSQL and Redis for first start, restart and concurrent starts;
  SIGKILL crashes mid-model and mid-tool call, across an approval pause, a workflow run and
  conversation turns, and five in a row; two processes deciding one approval; PostgreSQL and Redis
  outages and a database restart (failover); `pg_dump` backup and restore; schema upgrade from and
  rollback to `0.1.0a17`. A bounded soak drill (marker `soak`) runs with `ALGEN_DRILL_SOAK_RUNS`.
  `scripts/integration-tests.sh` exports `ALGEN_TEST_CONTAINER_ENGINE`,
  `ALGEN_TEST_POSTGRES_CONTAINER` and `ALGEN_TEST_REDIS_CONTAINER` for them. See
  docs/operations.md#persistence-restarts-outages-backup-and-rollback.
- `storage.postgres_connect_timeout_seconds` (default 10), `storage.postgres_command_timeout_seconds`
  (default 60, `null` for none; migrations are exempt) and `storage.redis_socket_timeout_seconds`
  (default 10), also as `PostgresDatabase(connect_timeout_seconds=..., command_timeout_seconds=...)`.
- `StoreUnavailableError` (`kind: unavailable`, retryable), raised by the PostgreSQL and Redis
  stores for connection, timeout and server-shutdown errors, and answered by the API with 503 and
  `Retry-After`. `RunConflictError`, a `ConflictError` the run stores raise when a checkpoint's
  version moved on.
- Signed policy bundles (`policies.bundles`, `policies.trusted_keys`, `policies.require_signed`):
  a YAML or JSON file of declarative rules (`deny_tools`, `require_approval_tools`,
  `deny_patterns`, `redact_patterns`) whose name, version, `issued_at` and `expires_at` are inside
  a detached Ed25519 signature (`<bundle>.sig`) verified at start-up against `env://` or `file://`
  public keys. With `require_signed: true` an unsigned bundle stops start-up; a signature that does
  not verify, an expired or future-dated bundle, and a version below the bundle's `min_version`
  (rollback) are always refused. Each bundle runs as the policy `bundle:<name>` for every agent,
  whatever its guardrail selection. Verifying needs the `encryption` extra; unsigned bundles do
  not. `algen-agent-runtime policy keygen|sign|verify` creates keys, signs and checks bundles.
  `CompositePolicyEngine.register(..., mandatory=True)` adds a policy every agent runs. See
  docs/operations.md#signed-policy-bundles.
- Explicit fail modes at every policy boundary: `policies.on_error` (`closed` or `open`),
  `policies.boundary_on_error` per boundary, and `policies.evaluation_timeout_seconds` (per
  policy, unset by default). `closed` refuses the value as a deny would (`reason_code:
  policy.error`); `open` skips only the failed policy. Each handled error writes a `policy.error`
  audit record (`fail_closed` / `fail_open`), adds to the `algen_agent_runtime.policy.errors`
  counter and logs `policy_error` with the error's type, never its text; the run's
  `policy.evaluated` event carries `fail_mode` and `error_count`. See
  docs/operations.md#boundaries-and-fail-modes.
- `tests/adversarial` (marker `adversarial`): scripted agent runs in which the mock model obeys an
  attacker, covering prompt injection, data loss, PII and secrets, and cross-tenant access through
  agent behaviour. It runs in the normal test suite.
- `network.egress_proxy`: send every outbound client the Runtime builds (`core.http`,
  `http_tools`, `remote_tool`, MCP HTTP transports and OAuth, OIDC discovery and JWKS, workflow
  notifications, `HTTPModelService` and the HTTP model providers) through one HTTP(S) proxy, with
  `no_proxy` hosts reached directly. `https://` targets go through a CONNECT tunnel, so TLS stays
  end to end. Each target passes the allowlist and private-network checks before the proxy is
  contacted; the proxied pool connects only to the proxy; proxy credentials are `env://`
  references and never appear in URLs, logs or errors. The proxy makes the final connection, so
  DNS pinning protects only up to it: give the proxy its own egress policy.
  See docs/operations.md#egress-proxy.
- `network.remotes`: per-host (or `*.domain`) CA bundle, mTLS client certificate and key (file or
  `env://`, never logged) and pinned server certificate SHA-256 fingerprints, for every outbound
  client, direct or through the proxy. `create_safe_http_client(outbound=...)`,
  `create_outbound_http_client` and `Container.outbound` expose the same settings to host code.
- `security.sandbox` (and a per-server `sandbox` on stdio MCP servers): environment allowlist,
  working directory, POSIX resource limits (CPU seconds, address space, open files, processes,
  file size) and an operator wrapper command (`bwrap`, `nsjail`, `firejail`, `sandbox-exec`) for
  MCP stdio servers and `core.subprocess`; `required: true` refuses to start a process without a
  wrapper. `core.subprocess` gains a wall-clock limit (`subprocess_timeout_seconds`, default 60)
  and kills the process group on timeout or cancellation. Defaults change nothing.
  See docs/operations.md#subprocess-and-mcp-stdio-sandboxing.
- Plugin signatures: `plugins.trusted_publishers` (Ed25519 public keys) and
  `plugins.require_signed`. A package's `algen-plugin-signature.json` signs its publisher, name,
  version, package name and the SHA-256 of every file, and is verified before the package is
  imported, for `domain_plugins` and workflow hook packages. `algen-agent-runtime plugin keygen`,
  `plugin sign` and `plugin verify`. Needs the `encryption` extra. See
  docs/operations.md#plugin-signing.
- Per-run limits on `budget`: `max_model_calls` (default 1,000; a continuation adds
  `step_increment`; the wrap-up notice counts it, `reason: model_calls`), `max_response_chars` per
  model response (default 1,000,000 characters of text plus tool-call arguments, enforced while a
  response streams) and `max_events` (default 100,000; past it only events that end or pause the run
  are recorded and the run fails at its next step). A run that ends at a limit fails with
  `run.failed` `reason: limit_exceeded` and `limit`, a `run.limit_exceeded` audit record and the
  `agent.run.limit_exceeded` span attribute; the existing step, token and cost limits report the
  same way. See docs/operations.md#run-limits.
- `runtime.stream_delta_interval_ms` (default 50): streamed model text is recorded as at most one
  `model.delta` run event per interval, so long streams do not fill the event store. `0` records
  every chunk; `AgentRuntime(stream_delta_interval_seconds=...)` defaults to 0.
- Per-tenant storage quotas, `storage.quotas` (`default` plus per-tenant overrides; unlimited by
  default): item and byte limits for artifacts, session memory and learned memory, enforced on
  write by the in-memory, PostgreSQL, S3 and (when a quota is set) Redis stores. A write past a
  limit writes nothing and raises `QuotaExceededError`; the API answers 413 with
  `code: QUOTA_EXCEEDED`. On PostgreSQL, migration `016_resource_bounds.sql` adds
  `algen_agent_runtime_tenant_usage`, kept by triggers in the same transaction as every write and
  backfilled from existing rows; a quota-checked write locks the tenant's usage row, so concurrent
  writes cannot overshoot. A full session-memory quota keeps a run's answer and skips the turn's
  memory (`memory.written` with `operation: skipped`). `GET /v1/storage/usage` (new scope `usage:read`) and
  `Container.storage_usage` report a tenant's usage and limits. See
  docs/operations.md#storage-quotas.
- Retention for conversations (with messages, events and feedback), finished workflow checkpoints,
  finished analytical graphs and completed or dead work items: `maintenance.conversation_retention_days`,
  `workflow_checkpoint_retention_days`, `analytical_graph_retention_days` and
  `work_item_retention_days` (all off by default), on PostgreSQL and in-memory stores, in batches
  like the existing purges. Conversations with a turn in progress or waiting, or a recent message,
  and child workflow runs whose parent is unfinished are kept. Migration 016 adds the supporting
  indexes. `CompositeRetentionSweeper` and `InMemoryRetentionSweeper` are new. See
  docs/operations.md#maintenance-and-data-retention.
- Example `pattern_side_effect_reconciliation`: a billing write whose response times out pauses
  its run (`tool_policy.on_indeterminate: pause`); the tool's `reconcile` hook finds the credit by
  idempotency key and the run resumes; an operator resolves another call `failed` with a reason
  over `/v1/tool-executions` (`tools:reconcile`); compensation runs on request (`compensating` to
  `compensated`); and a `ToolNotAttemptedError` refusal is recorded `failed`, not `indeterminate`.
  Offline, with a mock model and an in-process fake billing system.
- Example `pattern_secured_api`: the HTTP API in-process with `api.auth_mode: oidc` against a local
  JWKS stub and a key generated at start-up: scopes from claims, the approval inbox and its
  `approvals:read` scope, redacted `modified_parameters`, conversation ownership (another user of
  the tenant gets 404), token revocation by `jti`, and an `http_tools` entry with
  `auth.type: caller` that forwards the signed-in user's token through a host route and fails
  closed without one. Needs the `auth` extra; without it the example says how to install it and
  exits 0.

### Changed

- `core.http` in a built container now uses the SSRF-safe, DNS-pinned client (it used a plain
  client after validating the URL), and every container client honours `network` settings.
- `PluginManifest.signature` is documented as unused; signatures live in the package's signature
  file, which can be verified before import.
- `tool_policy.max_refusals_per_tool` counts refusals in a row: a call to the tool that runs
  successfully starts its count again. Set `tool_policy.refusal_count: per_run` to count every
  refusal in the run, as in 0.1.0a17. (#71)
- A tool call a guard refuses or holds back (`refused`, `repeated`, `over_limit`) emits
  `tool.started` with the guard's status before its `tool.refused` / `tool.repeated`, and its
  `tool.completed` carries `reason_code`. In a parallel reply, every call's `step.started` and
  `tool.started` are emitted in the order the model sent the calls, before any of them runs. (#71)
- The model router no longer falls back to another model when a provider rejects the request as
  invalid (HTTP 400/422 `invalid_request`); it raises that error, which does not count against the
  provider's circuit breaker. Authentication errors, rate limits, timeouts, 5xx and other 4xx such
  as 404 still fall back. HTTP 408 is now a retryable timeout.
- A tool call whose context carries caller credentials is never served from, or stored in, the
  tool-result cache: its result can depend on who is calling.
- The `ApprovalService` protocol adds `list(...)` and an optional keyword-only `agent` argument to
  `create`. A host's own implementation should add both. The approval redaction helper moved from
  `AgentRuntime._redact_mapping` to `algen_agent_runtime.security.redaction.redact_parameters`.
- PostgreSQL schema creation and migrations run under an advisory lock, so replicas starting at
  once against a fresh database apply them one at a time instead of failing.
- Stored approvals, run checkpoints and tool results keep the 0.1.0a17 shape unless a new feature
  is used (a pending steering message, a failed tool result), so a17 replicas can read them during
  a rolling deploy or after a rollback.
- PostgreSQL and Redis store calls fail with `StoreUnavailableError` instead of the driver's
  exception (asyncpg `ConnectionDoesNotExistError`, `OSError`, `TimeoutError`; redis-py
  `ConnectionError`, `TimeoutError`), and the API answers 503 instead of 500. A PostgreSQL command
  now times out after 60 seconds by default and a connection attempt after 10 (asyncpg's default
  was 60); a Redis connection or command after 10 seconds (no bound before).
- A run whose store becomes unavailable mid-run is no longer failed: it keeps its last checkpoint
  and the process drives it again once the store answers (or the next start recovers it), with the
  same caller credentials; `AgentRuntime.wait` follows it.
- A run drive whose checkpoint write loses the version check (`RunConflictError`, for example
  because another process recovered the run) stops without writing instead of marking the run
  `failed`.
- Configuration rejects `event_store`, `approval_store` or `tool_execution_store` `postgres` with a
  `run_store` other than `postgres`: their rows reference the run in PostgreSQL, so every run
  failed on its first event.
- A policy that raises or times out now ends its boundary with a `policy.error` deny
  (`PolicyDeniedError`) instead of its own exception propagating. Every boundary defaults to
  `closed`, which is what each already did (the run or the call failed), so no boundary becomes
  open unless configured.
- At one boundary a `deny` from any policy now wins over an earlier policy's
  `require_approval` or `require_clarification`: the later policies still run, so a call a
  policy bundle denies is refused instead of first being put to an approver.
- The `secrets` policy also redacts bearer tokens (`Bearer <token>` with a digit or separator in
  the token), and the `pii` policy redacts card numbers (`[REDACTED_CARD]`): digit runs with a
  card network's prefix and length (Visa, Mastercard, Amex, Discover, JCB, Diners, UnionPay) that
  pass the Luhn check, so timestamps and other long identifiers are left alone. As with any redaction of tool arguments, such a value in a tool call's
  arguments refuses the call at `before_tool`.
- `redact()` (audit metadata, logs, conversation records) also redacts `key=value` credential
  assignments, and opt-in telemetry content (`telemetry.include_content`) is also redacted for
  emails, phone, Aadhaar and card numbers.
- Migration 016 creates triggers on the artifact, memory and learned-memory tables, which locks them
  against writes until it commits; on large tables run it in a quiet period, and create its indexes
  `CONCURRENTLY` beforehand.

### Fixed

- The `pii` policy no longer redacts digit runs inside UUIDs, hashes and other tokens as phone or
  Aadhaar numbers (an identifier must not touch a letter, digit, `_` or `-`). About 1.7% of random
  UUIDs were altered, and because a redacted tool argument refuses the call, roughly one in sixty
  tool calls carrying a run, record or order UUID was refused at `before_tool`.
- Every `/v1/runs/{run_id}` route (read, narrative, events, cancel, clarification and approval)
  enforces conversation ownership: a run started for a conversation turn answers `404` to any
  other user of the tenant. Before, any `runs:write` principal in the tenant could decide or cancel
  another user's conversation-turn run, and any `runs:read` principal could read it, given its id.
- Declared HTTP tools refuse a path value that is empty, `.` or `..` (also percent-encoded).
  Before, a model argument could move the call, and a forwarded credential, to another path on the
  same host.
- A query string in an HTTP tool's `url` template is no longer dropped; it is merged with the
  `query` templates and pagination parameters.
- Gemini: the `gemini` adapter merges the leading system messages (instructions, retrieved
  evidence, pinned compaction) into one, because Gemini's OpenAI-compatible endpoint keeps only the
  last system message and agents lost their instructions whenever retrieval ran. A later system
  message, such as the verification-repair instruction, is sent in place as a user message
  prefixed `[system] `.
- `telemetry.otlp_endpoint` no longer installs a meter provider with no reader. Before, every
  Runtime metric was recorded and silently dropped, and the process-global provider was taken, so
  an application could not install its own afterwards.
- A run recovered after a crash no longer counts the interrupted model or tool step twice, so
  repeated crashes no longer use up `max_steps` and fail the run.
- Resuming a run right after it paused for approval could record the decision and then refuse to
  drive the run (`run is already executing`), leaving it stuck until a restart: `resume` now waits
  for the drive that paused the run to finish, and decides nothing if it is still running.
- After a PostgreSQL restart or failover, only the first call fails: the pool replaces all its
  dead connections at once instead of failing one call per pooled connection.
- Container shutdown closes resources with `aclose()` when they have it, so a Redis client no
  longer warns that `close()` is deprecated.
- A `deny` decision at `plan_tool` refuses the call; before, only `require_approval` was honoured
  there and a denial was ignored until `before_tool`, after an approval could have been asked for.

### Security

- A model reply that reuses an earlier tool call's id (or repeats an id within the reply) gets a
  fresh id. Before, a new call under an approved call's id was treated as that call: it skipped
  approval and was answered with the recorded result without running, and the model got no result
  for it.
- Approval requests (`redacted_parameters`, the pause payload and `approval.required`) redact
  bearer tokens and `key=value` credentials inside argument values, not only top-level keys named
  like secrets.
- The text of a runtime error a tool raised (shown to the model with `return_to_model`, or kept as
  the run's error) is redacted for credentials; tools write these messages themselves.
- Opt-in telemetry content no longer carries a model reply's credentials or personal data before
  the `after_model` policy redacts the reply.
- A `proxy=` passed to `create_safe_http_client` was used without any check of the proxy host
  against the IP pinning, and could route requests around the allowlist. It is now refused
  unless it is the configured `network.egress_proxy`, and `mounts=` is refused.

### Examples

- `examples/quickstart_conversation`: a streamed two-turn conversation through `ConversationService`
  and `RuntimeConversationHandler`, offline. It prints `assistant.message.delta` events, a
  `tool.note` from `ToolContext.emit_note`, a `runtime.steer` message sent while a tool runs, and a
  turn that pauses for approval and resumes (`--reject` to reject it).
- `examples/pattern_guarded_tools`: typed tools (`@tool`, `build_tool`) behind a `precheck`, a
  failure returned as data (`failure_field="ok"`), `cache_context={"user_id"}` with the tool-result
  cache on, `wrap` middleware that keeps the hooks, `tool_policy` guards (consecutive
  `max_refusals_per_tool`, `max_calls_per_tool`, `repeat_guard`), `wrap_up_policy`, and a
  `UsageMeter` that charges from `MeterCall` with `usage_meter_margin` and refuses a user with no
  allowance. Offline; prints what each control did.
- `--offline` for `quickstart_rag`, `quickstart_cloud_chat`, `pattern_text_to_sql` and
  `case_study_teaching_assistant` (the same `agent.yaml` with the hosted provider swapped for the
  mock provider and a scripted reply, `examples/offline.py`) and for `pattern_provider_fallback`
  (the former `--deterministic`, still accepted). Without `--offline` they use the real provider
  as before and stop with a hint when its key is missing.
- `pattern_text_to_sql` and `case_study_teaching_assistant` run without the `traccia` extra:
  `telemetry.traccia.enabled` is now `false` in their `agent.yaml`. Turn it on with
  `ALGEN_AGENT_RUNTIME__TELEMETRY__TRACCIA__ENABLED=true`; the teaching assistant's
  `config/ecs.yaml` still enables it for the ECS deployment.
- `pattern_provider_fallback` printed its route from `RunResult.model_used`, which does not exist,
  so a successful live run raised `AttributeError`; it now reads the run's last `model.completed`
  event.
- `tests/integration/test_all_examples_run.py` runs every runnable example module as a subprocess,
  offline and non-interactively, in parallel with a 60-second timeout each. A runnable module that
  is neither run nor listed as skipped (Ollama, pgvector, the HTTP dashboards, the MCP server
  process) fails the test. The never-collected `examples/quickstart_rag/tests/test_smoke.py` moved
  to `tests/end_to_end/test_rag_example.py`.
- Example READMEs: the `--offline` paths, the extras the MCP and OpenAI Agents examples need, the
  local chat test command, and `docs/rag.md`'s path to the pgvector Text-to-SQL example.

### Upgrade notes

- Migrations `015_approval_agent.sql` and `016_resource_bounds.sql` run on startup. Migration 016
  locks the artifact, memory and learned-memory tables until it commits; run the upgrade in a
  quiet period on large tables.
- Clients that retry only on 500 should also retry on 503: store outages now answer 503 with
  `Retry-After`.
- A PostgreSQL command that needs longer than 60 seconds needs `storage.postgres_command_timeout_seconds`
  raised; Redis calls time out after 10 seconds (`storage.redis_socket_timeout_seconds`).
- Pass credentials to tools as `env://` references or caller credentials, not as argument values: a
  bearer token or card number in a tool call's arguments now refuses the call.
- `core.http` refuses private and internal addresses unless they are allowlisted in `network`, and
  `core.subprocess` stops a command after 60 seconds.
- A host's own `ApprovalService` implementation should add `list(...)` and the keyword-only `agent`
  argument to `create`.

### Known limitations

- Conversation turns do not expose steering yet; a host steers the turn's run by its id
  (`pending.run_id`). Steering a run that another worker is driving is not supported.
- Workflow nodes, declared HTTP tools and MCP tools (`isError`) do not yet return a failure as data
  the way `failure_field` does for Python tools.
- P0 items are complete for the single-node scope; multi-node operation, HA failover and
  cross-region recovery remain out of scope (see `docs/production-readiness.md`).

## [0.1.0a17] - 2026-10-28

### Added

- `api.auth_mode: oidc`: bearer JWTs verified against OpenID Connect issuers' JWKS
  (`api.oidc.issuers`). Keys are discovered from `/.well-known/openid-configuration` or a
  configured `jwks_url`, fetched through the SSRF-safe client from the issuer host only (plus
  `additional_allowed_hosts`), cached for `jwks_cache_ttl_seconds`, refreshed on an unknown `kid`
  at most once per `jwks_min_refresh_interval_seconds`, and kept when a refresh fails. Only
  asymmetric algorithms bound to the key type are accepted; `iss`, `aud`, `exp`, `nbf`, `iat`
  are validated with bounded `leeway_seconds`; `max_token_lifetime_seconds` and
  `required_claims` are optional. See docs/operations.md#api-authentication.
- Claim mapping per issuer (`claims`): subject claim, tenant claim or `fixed_tenant`,
  `allowed_tenants`, `scope`/`scp` scope claims, and `role_claims` + `role_scopes` mapping roles
  or groups to Runtime scopes. Nested (`realm_access.roles`) and namespaced claim names work. A
  missing tenant fails closed; a token's own `*` scope is dropped.
- Optional token revocation (`api.token_revocation`) for `jwt` and `oidc`: a tenant-scoped `jti`
  denylist and a per-subject "revoked before" cutoff, in memory or PostgreSQL (migration
  `013_token_revocation.sql`). Revoke through `POST /v1/auth/revocations/tokens` and
  `POST /v1/auth/revocations/subjects` (scope `auth:revoke`) or `Container.token_revocations`.
  Token introspection (RFC 7662) is not implemented.
- Trusted-proxy service identity (`api.trusted_proxy_identity`, off by default): a client
  certificate identity forwarded in a header by a TLS-terminating proxy is honoured only from
  configured proxy CIDRs and mapped to a configured tenant, user and scopes; the header from any
  other peer is refused and audited.
- Authentication audit records `auth.authenticate` (every failure; successes with
  `api.authentication_audit.record_success`), `auth.audit.suppressed`, `auth.token.revoked` and
  `auth.subject.revoked`, the `algen_agent_runtime.auth.attempts` metric, and an
  `authentication failed` log. They carry at most the method, reason, issuer, key id and
  verified subject, never the token.
- `algen_agent_runtime.auth` package: `ApiAuthenticator`, `OidcVerifier`, `JwksCache`,
  `TokenRevocationStore` with `InMemoryTokenRevocationStore` and `PostgresTokenRevocationStore`,
  `TrustedProxyIdentity`, `AuthenticationAuditor`. `principal_dependency` takes keyword-only
  `audits`, `revocations` and `oidc_transport`; `Container` has `token_revocations` and
  `oidc_transport`.
- Reconciliation of tool calls whose outcome is unknown. `runtime.reconciliation`
  (`ToolReconciliation`) lists a tenant's `indeterminate` and stale `started` calls (by status,
  run, tool and age), resolves one as `completed` (optionally with the result, checked against the
  output schema, and an external `result_reference`) or `failed`, asks the tool's `reconcile` hook,
  runs the tool's compensation, and resumes a run left waiting on a settled call. Each decision is
  a compare-and-set on the ledger status, records who decided and why in the record's new
  `resolutions`, writes a `tool.reconcile` or `tool.compensation` audit record and emits a
  content-free `tool.reconciled` or `tool.compensation.*` run event. HTTP:
  `GET /v1/tool-executions`, `GET /v1/tool-executions/{key}` and `POST .../resolve`,
  `.../reconcile`, `.../compensate`, `.../resume`, all tenant-scoped and requiring the new
  `tools:reconcile` scope; responses never carry result values. See
  docs/operations.md#resolving-indeterminate-tool-calls.
- `tool_policy.on_indeterminate: pause` (default `on_error`, unchanged behaviour): a
  side-effecting call with an unknown outcome pauses the run in the new
  `RunStatus.AWAITING_RECONCILIATION` (a paused status) with a `tool.reconciliation_required`
  event; settling the call continues the run with the recorded result or a tool error.
- `Tool(reconcile=..., reconcile_on_replay=True)` and `ReconcileOutcome`: an optional hook that asks
  the external system, by idempotency key, whether a call took effect. Operators' auto-reconcile
  uses it, and so does the executor before replaying a `started` or `indeterminate` call. `@tool`
  and `build_tool` take `reconcile=` and `compensation=`; `describe` shows both.
- `Tool.compensation` now runs, only on an operator's explicit request, at most once per request:
  ledger statuses `compensating`, `compensated` and `compensation_failed`. Nothing is compensated
  automatically.
- `ReconcilableToolExecutionStore` (`get`, `find`, `transition`), implemented by the in-memory and
  PostgreSQL ledgers; `ToolExecutionRecord.created_at`, `updated_at` and `resolutions`;
  `ToolExecutor.executions`.
- Migration `014_tool_execution_reconciliation.sql`: a `NOT VALID` check constraint on ledger
  statuses and an index on `(tenant_id, status, updated_at)`.

### Changed

- With `auth_mode: jwt`, authentication failures are now recorded as `auth.authenticate` audit
  records (under tenant `_authentication`, capped at 600 per minute per process) and 401
  responses carry `WWW-Authenticate: Bearer error="invalid_token"`. Response bodies and the
  verification itself are unchanged. Set `api.authentication_audit.record_failures: false` to keep
  the previous audit volume.
- A request is now authenticated once even when API rate limiting authenticates it before the
  route.
- `algen-agent-runtime serve` disables uvicorn's `X-Forwarded-For` peer rewriting when
  `api.trusted_proxy_identity` is enabled.
- The ledger's `complete` and `fail` only change a `started` record, so a call that finishes after
  an operator settled it cannot overwrite the decision (logged as
  `tool_execution_late_outcome_ignored`). Every built-in path already called them on `started`
  records only.
- Retention also keeps runs with `compensating` or `compensation_failed` tool executions.
- Upgrade note: custom `ToolExecutionStore` implementations keep working, but reconciliation needs
  the `ReconcilableToolExecutionStore` methods. Clients that switch on `RunStatus` may see
  `awaiting_reconciliation` once an agent opts into `on_indeterminate: pause`.

### Security

- `api.jwt_algorithms` may no longer include `none`.

### Known limitations

- Token introspection (RFC 7662) is not supported; revocation uses the `jti` denylist and the
  per-subject cutoff. mTLS terminated by the Runtime itself is not mapped to principals; use the
  trusted-proxy identity hook.
- The in-memory revocation store is per process: deployments with more than one worker need
  `token_revocation.store: postgres`.
- Compensation runs only on an operator's request. HTTP API, email and MCP tools have no
  reconcile hook yet, and workflow tool steps do not pause for reconciliation.

## [0.1.0a16] - 2026-10-28

### Added

- Explicit readiness dependencies. `/health/ready` now reports every dependency under
  `dependencies` (`store.<kind>` for each configured store, `cache.redis`, `provider.<id>`,
  `retriever.<name>`, `mcp.<server>`, `telemetry.otlp`/`telemetry.traccia`, and
  `resource.<TypeName>` for plugin resources), each with `kind`, `required`, `status`
  (`up`/`down`/`unknown`), `backend` for stores and a content-minimal `reason`, plus
  `degraded_dependencies`, `failing`, `ready` and `lifecycle`. The `status`, `providers` and
  `stores` keys stay. Probes run concurrently and are bounded by a timeout. See
  docs/operations.md#readiness-and-lifecycle.
- `runtime.readiness` settings: `model_providers` (`any`, `all` or `none`),
  `require_durable_stores`, `required`, `optional` and `probe_timeout_seconds`.
- Workflow recovery in the service lifecycle. Runs started through the HTTP workflow API record
  `owner: "workflow_service"` (new optional `WorkflowExecutionState.owner`), and container start
  recovers the interrupted ones with the exact configured manifest and trusted hooks
  (`runtime.recover_service_workflows`, default `true`). `WorkflowService.recover`,
  `expire_paused`, `drain` and `idle`; `Container.attach_workflows`.
- `MultiAgentWorkflowExecutor.expire_pause(...)` expires an approval wait past its `expires_at`,
  or an input wait older than `input_timeout_seconds`, through the engine's own expiry path,
  including waits inside child workflows; new event `workflow.input.expired`. Also
  `MultiAgentWorkflowExecutor.pause_expired`, `manifest_matches`, the `interrupted` constructor
  argument and `run(..., owner=...)`.
- The maintenance sweep expires paused workflow approval and input waits of service-owned runs
  (`maintenance.expire_paused_workflows`, default `true`; input waits only with
  `clarification_timeout_seconds`), reported as `expired_workflow_pauses`.
- Container lifecycle phases (`Container.lifecycle.phase`: `created`, `starting`, `ready`,
  `draining`, `stopped`); `/health/ready` returns 503 with `status: draining` from the start of
  shutdown.
- Repeated start/run/drain/restart cycle tests on in-memory stores and on live PostgreSQL.
- Conversation turns stream, pause for approval or input, and resume (#50).
  - The `runtime` conversation handler streams a turn's run when every model the agent may use can
    stream, and forwards text deltas as `assistant.message.delta` events (`{message_id, delta}`),
    coalesced to at most one per `conversation_streaming.delta_interval_ms` (default 50) and at
    most `delta_max_chars` (default 2048) characters each. Deltas are not audited. A non-streaming
    model gives only the final message, as before.
  - A turn whose run awaits an approval or a clarification pauses: the assistant message moves to
    the new `awaiting_approval` or `awaiting_input` status, with a `pending` action (approval id,
    tool, proposed action, risk, expiry, options, redacted parameters, the tool's parameters
    schema, or the question) in the message and in `assistant.message.awaiting_approval` /
    `assistant.message.awaiting_input` events. No raw tool arguments are exposed.
  - `ConversationService.respond(conversation_id, turn_id, tenant_id, user_id, response, *,
    credentials=None)` with `ConversationApprovalDecision` (approved, rejected, modified) or
    `ConversationInputAnswer`, and the routes `POST /v1/conversations/{id}/turns/{turn_id}/approval`
    and `.../input` (scope `conversations:write`). The answer resumes the run and keeps streaming
    into the same assistant message (`assistant.message.resumed`, with `decided_by`).
  - New `expired` message status and `assistant.message.expired` event for a turn whose approval or
    clarification expired. Cancelling a pending turn (`DELETE .../active-response`) cancels its
    run.
  - Pending turns survive a restart: they are stored on the assistant message, `recover()` keeps
    them waiting, and `ConversationService.reconcile_pending_turns()` (also run by the maintenance
    sweep, reported as `reconciled_pending_turns`) ends turns whose run ended while they waited and
    follows, never re-runs, a run that was resumed just before a restart.
  - `ConversationService.submit(..., credentials=...)` and `respond(..., credentials=...)` pass the
    caller's credentials to the turn's run, in memory only, as for `Runtime.start` and `resume`.
    The HTTP routes do not forward headers.
  - `ConversationPendingAction`, `ConversationApprovalDecision`, `ConversationInputAnswer`,
    `ConversationTurnResponse`, `PendingTurnExpiredError` and `ResumableConversationHandler`
    exported from `algen_agent_runtime.conversations`; `ConversationTurnResult` takes the outcomes
    `awaiting_approval`, `awaiting_input`, `cancelled` and `expired` with `pending`.
  - `ModelRouter.supports_streaming(profiles, allowlist)`.
  - `conversation_streaming` settings (`enabled`, `delta_interval_ms`, `delta_max_chars`).
  - See docs/operations.md#conversation-turns-streaming-approvals-and-input.

### Changed

- `/health/ready` returns 503 only when a required dependency is down. By default at least one
  model provider must be healthy (previously every provider), and a Redis cache, retrievers,
  optional MCP servers and telemetry exporters no longer fail readiness (they make it 200 with
  `status: degraded`). Set `runtime.readiness.model_providers: all` to keep requiring every
  provider, and list other dependencies under `runtime.readiness.required`.
- Shutdown drains the workflow service for `shutdown_grace_seconds` before the runtime, and leaves
  unfinished service workflow runs recoverable instead of marking them `cancelled`; the agent run a
  workflow step waits on is drained by the runtime rather than cancelled. Starting or answering a
  workflow run during shutdown returns 409.
- Upgrade note: workflow runs started through the HTTP API before this release have no owner, so
  they are neither recovered on start nor expired by the sweep; decide or answer them through the
  API, or recover them once with `executor.recover`.
- Conversation turns of the `runtime` handler stream by default (`stream=True` on their runs) when
  the agent's models can stream, so they now emit `assistant.message.delta` events, and their
  model calls bypass the model-response cache as streaming runs always have. Set
  `conversation_streaming.enabled: false` to keep the previous behaviour.
- A conversation turn whose run pauses for approval or clarification now waits in
  `awaiting_approval` / `awaiting_input` instead of completing with an empty message, and a new
  message on that conversation gets `409` until the turn is answered or cancelled. A turn whose
  run is rejected ends `failed` with the Runtime's "Proposed action was rejected." text, and one
  whose run is cancelled ends `cancelled`.

### Known limitations

- Pending conversation turns are reconciled and their events fanned out per process, like the rest
  of the conversation layer; run one Runtime process per deployment.
- Readiness reports OTLP and Traccia exporters as `unknown`: they have no health API.
- Workflow runs started through the HTTP API before this release have no `owner`, so they are
  neither recovered on start nor expired by the maintenance sweep.

## [0.1.0a15] - 2026-10-28

### Added

- `MeterCall`, exported from `algen_agent_runtime`: what a usage meter is told about each model
  call. It carries the tenant, user, run, parent run, session, conversation, turn, agent and
  agent version, the step id, the `purpose` (`step`, `compaction` or `other`), the retry
  `attempt`, the `provider` and `model` of the candidate being tried, whether it is
  `streaming`, and a read-only copy of `RunRequest.metadata`. The runtime builds it from the
  persisted run, so resumed and recovered runs report the same tenant, user and run. See
  docs/agent-tooling.md#metering-model-calls.
- `ModelRouter.generate(..., meter_call=...)` and `ModelRouter.stream(..., meter_call=...)`.
  The router fills in `provider`, `model` and `streaming` for each candidate, so a fallback
  reports the model it tries. Without `meter_call`, the meter gets `purpose="other"` and no run
  fields.
- `provider` and `model` in the `budget.refused` event data and audit record, and as
  attributes of `MeterRefusedError`, which the router fills before re-raising.
- Per-run tool credentials. `Runtime.start`, `run` and `resume` take a keyword-only
  `credentials: Mapping[str, str] | None`, and tools read them from the new
  `ToolContext.credentials`: a read-only private copy, empty by default. They are kept in memory
  only, while the run executes, and dropped when it pauses or ends; they never reach run state,
  checkpoints, stores, events, audits, spans, logs, caches or the model, and are left out of
  `ToolContext`'s `repr`, `model_dump`, equality and hashing. A resumed run sees only the
  credentials passed to `resume`; one resumed without them or recovered after a restart sees none.
  `core.subagent` passes the parent's credentials to its child run. Workflow tool steps and the
  HTTP API pass none. `fake_tool_context(credentials=...)` sets them in tests. See
  docs/tool-authoring.md#credentials-of-the-caller and docs/threat-model.md.

### Changed

- Allow structlog 26 (`structlog>=24.4,<27`). structlog 26 drops only Python 3.8 and 3.9 support.
- `UsageMeter.reserve` takes `call: MeterCall` as a keyword argument. `settle` and `release` are
  unchanged; keep the `MeterCall` in the ticket to use it when settling.
- The `Runtime` protocol (`types.interfaces.Runtime`) adds the optional `credentials` keyword to
  `start`, `run` and `resume`. A host's own implementation of the protocol should accept it.
- `ToolContext` validation errors no longer echo the rejected input, and `ToolContext` is now
  hashable (from its identifiers; secrets and credentials take no part).

- The bundled agent skills (`.agents/skills`) cover 0.1.0a14 and 0.1.0a15: tool-call guards,
  shortened older tool results, caller credentials, MCP OAuth and `streamable_http`, document
  inspection, the Traccia default and usage metering with `MeterCall`, so agents built with
  them use these features.

### Deprecated

- The 0.1.0a14 `UsageMeter.reserve(*, max_input_tokens, max_output_tokens)` form, without
  `call`. The router still calls such a meter, without `call`, and emits one
  `DeprecationWarning` per meter, attributed to the meter's module. Removed in 0.2.

### Upgrade notes

- Usage meters: add `call: MeterCall` to `reserve`; nothing else changes.

### Known limitations

- Workflow tool steps get no credentials, and the HTTP run and resume endpoints do not forward
  any: hosts pass `credentials=` through the Python API.
- Credentials are kept in memory only, so a run recovered after a restart, or resumed without
  them, gives its tools an empty mapping.
- A child run inherits credentials only through `core.subagent`; a host's own delegation tool
  passes `credentials=context.credentials`.

## [0.1.0a14] - 2026-10-28

### Added

- Tool-call guards from the Algen Agent Harness, kept in `RunState` so they survive an approval
  pause and a restart (`tool_refusals`, `tool_call_counts`, `seen_read_calls`; a checkpoint
  without them still loads). All but the schema check are off by default. See
  docs/agent-tooling.md#tool-call-guards and, for events, audits and spans,
  docs/operations.md#tool-call-guards.
  - `Tool(..., precheck=...)` and `@tool(..., precheck=...)`:
    `async (definition, arguments, earlier) -> str | None`, run for every call that is not
    read-only, before any approval is asked for. A refused call never runs and nobody is asked; it
    is answered with `{"ok": false, "error", "note"}` and status `refused`, with a `tool.refused`
    event, a `tool.call`/`refused` audit and an `agent.tool.call` span. `earlier` holds the calls
    planned before it in the same parallel reply that will run.
  - Arguments are checked against the tool's `input_schema` before the call runs or is put to
    the user, for every tool (`tool.schema_invalid`).
  - `tool_policy.max_refusals_per_tool`: from that many refusals of one tool in a run, the note
    tells the model to stop calling it and tell the user what was refused and why.
  - `tool_policy.repeat_guard` and `tool_policy.max_calls_per_tool`: a read repeated with no
    successful change since, or a call past the tool's limit, returns a note instead of running
    (`tool.repeated` event, statuses `repeated` and `over_limit`). `ToolDefinition.volatile`
    (also `@tool(volatile=True)` and `http_tools[].volatile`; the live `starter.clock` sets it)
    marks reads that are never held back.
  - `wrap_up_policy` (`enabled`, `tool_calls_left`, `token_wrap_up_share`,
    `token_answer_share`): near the end of the run's steps or token budget, each tool message
    carries a wrap-up notice as a separate text block (`tool.wrap_up` event,
    `agent.wrap_up.tool_calls_left` span attribute). No notice while automatic continuation
    extensions are left.
- OAuth for remote MCP servers (off by default): `auth: {type: oauth, ...}` on `streamable_http`
  and `sse` servers (`OAuthClientAuth`) runs the authorization code flow with PKCE S256 through
  `POST /v1/mcp/servers/{server}/oauth/start`, `GET /v1/mcp/oauth/callback`,
  `GET .../oauth/status`, `GET /v1/mcp/oauth/servers` and `DELETE /v1/mcp/servers/{server}/oauth`
  (scopes `mcp:read`, `mcp:connect`, and `mcp:admin` for tenant-wide connections). Endpoints come
  from RFC 9728/8414 discovery or config; client secrets are `env://` references, and public
  clients use PKCE alone. Tokens are kept per tenant (`token_scope: tenant`, the default) or per
  user (`token_scope: user`, which fails closed without a `user_id`), refreshed before expiry and
  once on a 401, and revoked at the provider (RFC 7009) when it offers that. A call with no
  connection returns a clean `MCPNotConnected` tool error to the model whatever `on_error` says,
  and emits an `mcp.auth_required` event with the start route for a Connect button. Storage is
  `storage.mcp_oauth_token_store`: `memory`, or `postgres` (migration `012_mcp_oauth.sql`), which
  encrypts tokens with AES-256-GCM bound to their tenant row and needs
  `storage.mcp_oauth_encryption_key` and the new `encryption` extra (`cryptography`). New names
  in `algen_agent_runtime.mcp` (`OAuthClientAuth`, `MCPOAuthTokenScope`, `MCPNotConnectedError`,
  `MCPOAuthManager`, `MCPTokenStore`, `InMemoryMCPTokenStore`, `PostgresMCPTokenStore`) and
  `algen_agent_runtime.security.encryption` (`SecretCipher`, `AesGcmCipher`). Servers without
  `auth` are unchanged. Dynamic client registration is not supported yet. See docs/mcp.md.
- `context_policy.shorten_older_tool_results` (off by default) with
  `shortened_tool_result_chars` (1200): when an older tool exchange no longer fits the context
  budget, its long text results are cut to their opening and marked as shortened instead of the
  exchange being dropped, so a long run keeps a record of what it already looked up. The call
  stays with its results; only the request sent to the model changes. `pack_messages` takes the
  matching `shorten_tool_results_to` keyword, and `DefaultContextBuilder.pack` an optional
  `ContextPolicy`, passed by `build()` only when shortening is on so existing
  `pack(self, messages)` overrides keep working. The final packing against a model's
  `context_window_tokens` does not shorten. See docs/artifacts.md.
- Document inspection for Excel (`XlsxDocumentExtractor`), PowerPoint (`PptxDocumentExtractor`)
  and HTML (`HtmlDocumentExtractor`), and CSV/TSV through the plain-text extractor;
  `extractor_for` returns them for `.xlsx`, `.pptx`, `.html`/`.htm`, `.csv` and `.tsv`. Standard
  library only. Hidden sheets, rows, columns, slides, shapes and HTML elements are kept out of
  the sanitized text and reported as `hidden_text.*` findings; Office packages get the DOCX
  limits (part count, size, compression ratio, no DTD) and the macro and remote-content findings.
  XLSX date and time cells come out as ISO dates and times, a sheet longer than
  `max_rows_per_sheet` is reported as `document.truncated`, and cells without an `r` reference
  follow the previous cell. Known gaps are listed under "Known limitations".
  The shared package checks are in `security/ooxml.py`. See docs/document-security.md.
- Usage metering for every model call: `build_container(..., usage_meter=...)` (and
  `ModelRouter(usage_meter=...)`) takes a `UsageMeter` (`reserve(*, max_input_tokens,
  max_output_tokens) -> ticket`, `settle(ticket, usage)`, `release(ticket)`, all async; in
  `algen_agent_runtime.models.metering` and exported from `algen_agent_runtime`). The router
  reserves the request's `estimate_request_tokens` and output cap before each provider dispatch
  and settles the reported usage after it, so steps, retries, fallbacks, repairs, streamed calls
  and compaction are charged; a failed, errored or cancelled call is released. A meter refuses
  by raising `MeterRefusedError`: it is not retried, not passed to a fallback model and not
  counted against provider health, and the run fails with the reason on `RunState.error`, a
  `budget.refused` event and audit record, `run.failed` with `reason: budget_refused`, and
  `budget.refused` on the `agent.run` span. Without a meter nothing changes. Replaces the Algen
  Agent Harness's `metering.py` (`CreditMeter` + `MeteredProvider`): pass the same meter object
  to `build_container`, raise `MeterRefusedError` instead of any exception to refuse, and read
  the reason from the run's `error` instead of `MeteredProvider.refusal`. See
  docs/agent-tooling.md and docs/operations.md.

### Changed

- `extractor_for` names every supported format in its `ValueError` for an unknown suffix.
- With Traccia on, Traccia receives only the traces the Runtime started. Spans that other libraries
  create through the global OpenTelemetry provider (FastAPI's request tracing, httpx or SQLAlchemy
  instrumentors), which Traccia filed under `unknown-agent-name`, are dropped. A trace is kept when
  the span that opened it in this process is a Runtime root: `conversation.turn` or an `agent.run`
  that opens its trace, now marked `algen.runtime.root=true`, or a span from a Runtime tracer
  (`algen_agent_runtime.*`), so Runtime spans without a run (model services, feedback) still go out.
  A run started inside an HTTP request now opens its own trace instead of becoming the request's
  child. New setting `telemetry.traccia.export_foreign_traces` (default `false`); `true` restores
  the previous behaviour. Works with and without `export_whole_runs`. See docs/operations.md.
  **Upgrade note:** apps that turned off FastAPI's request tracing to keep it out of Traccia
  (`FastAPI(telemetry={...: False})`) can remove that workaround after upgrading.
- A tool call whose arguments do not fit the tool's input schema is now refused before it runs
  or any approval is created. With `tool_policy.on_error: return_to_model` the model gets
  `{"ok": false, "error": "Invalid arguments for <tool>: ...", "note": ...}` (status `refused`)
  instead of a `ToolExecutionError`; a `fail_run` agent still fails, without first pausing for an
  approval of a call that cannot run.

### Upgrading from the Algen Agent Harness

- `prechecks.py` → `Tool.precheck`, the built-in schema check (Studio's `schema_problem` can be
  dropped from its precheck) and `tool_policy.max_refusals_per_tool: 3` (the harness's
  `MAX_REFUSED`). `Refusals.planned`/`unplan` and the serial lock are not needed: the Runtime runs
  calls that need approval one at a time, in the model's order, and passes a parallel reply's
  earlier calls as `earlier`. The precheck now also runs for non-read calls that need no approval.
- `policies.py` `ApprovalRules` → the existing `approval_policy` and the `side_effects` policy at
  `plan_tool`: set `require_for_side_effects`/`require_for_destructive` and mark the tools that
  must ask with `ToolDefinition.requires_approval` (or `write(approval=True)`); a refusal is no
  longer smuggled through as an `ALLOW` decision.
- `repeats.py` → `tool_policy.repeat_guard`, `tool_policy.max_calls_per_tool` (was
  `turn_call_limits`) and `ToolDefinition.volatile` (was `repeatable_tools`).
- `allowance.py` → `wrap_up_policy`. The notice is a second text block on the tool message, not
  `tool_calls_left`/`wrap_up` keys merged into a dict result.
- `metering.py` `CreditMeter` + `MeteredProvider` → `UsageMeter` passed to
  `build_container(usage_meter=...)`. The router meters every call it dispatches, so there is no
  provider to wrap. A meter refuses by raising `MeterRefusedError` (not its own exception), and
  the reason is on `RunState.error` and the `budget.refused` event (was `MeteredProvider.refusal`).
- Counts are per run, which is one turn of a Studio conversation; the notes keep the harness's
  "in this turn" wording.

## [0.1.0a13] - 2026-10-27

### Added

- Workflows over HTTP: `GET /v1/workflows`, `POST /v1/workflows/{name}/runs`,
  `GET /v1/workflows/runs/{id}`, and `.../approval` and `.../input` to answer a waiting step, so
  another system can start a deployed project's workflows and decide their approvals. Hook code
  loads only from `security.trusted_plugin_prefixes`. `workflow_notifications` posts a signed
  event when a run waits or ends. See docs/multi-agent-workflows.md.

### Fixed

- A dotted tool name no longer ends a run when a model shortens it. Providers that cannot take
  dots in function names (Anthropic, OpenAI-compatible) now get a readable alias
  (`hr.find_people` is sent as `hr__find_people`) when it fits and no other tool has it, and the
  hash-suffixed alias only otherwise. A name a model returns maps back when it is the Runtime name
  itself, or a hashed alias without its suffix, and exactly one tool matches.
- A call to a tool that does not exist is answered with a tool error naming the agent's tools
  (`UnknownTool`), and the run continues, whatever the agent's `tool_policy.on_error`. Before, the
  run failed with `PolicyDeniedError`. A tool that exists but is not enabled for the agent is
  still a policy breach.

## [0.1.0a12] - 2026-10-27

### Removed

- The Gmail adapter: `communications.gmail`, `GmailMailboxClient`, `GmailMailboxPage`,
  `GmailOAuthSettings`, `InboundEmailAttachment`, `InboundEmailSummary` and `gmail_inbox_tool`.
  Its receipts listed Bcc recipients as accepted although they were never delivered. The Gmail
  connector now lives in `algen-agent-tools` (`algen_agent_tools.gmail`), which delivers Bcc.
  Importing a removed name from `communications` raises an `ImportError` that names the
  replacement.
- The `case_study_responsible_hiring` and `pattern_text_to_sql` examples are no longer installed
  with the wheel. They stay in the repository and sdist; the other examples, which Studio imports,
  are unchanged.

### Fixed

- `@tool` tools refuse undeclared arguments when called directly through `Tool.execute`. The
  advertised input schema already closed every object and the executor enforced it, but a direct
  call let a Pydantic model that ignores unknown fields drop them silently.
- The default `pii` guardrail no longer refuses every email tool. A tool argument whose schema
  declares `"format": "email"` and holds exactly one address is left as is; all other arguments
  are still redacted. The deprecated `EmailAddress` declares the format, so `email_tool` works
  under default guardrails.

### Deprecated

- `algen_agent_runtime.communications` (`EmailMessage` and the other message contracts,
  `SmtpEmailSender`, `SmtpSettings`, `email_tool`, `GmailMailboxClient`, `gmail_inbox_tool`) moved
  to the `algen-agent-tools` package and now raises a `DeprecationWarning` on import, attributed
  to the importing module. It keeps working until it is removed in 0.2.
- `tools.adapters.database_tool`, which took a raw statement from the model, raises a
  `DeprecationWarning`; declare typed queries with `algen_agent_tools.postgres.PostgresToolkit`.
  `remote_tool` and `mcp_tool` stay: they are the Runtime's HTTP and MCP adapters.

### Upgrade notes

- Gmail users: `pip install "algen-agent-tools[gmail]"` and use `gmail_toolkit` (plugin
  `algen_agent_tools.gmail`); the tools are `gmail.list_messages` and `gmail.send_email`.
- Agents that set `guardrail_policy.tool_policies` without `pii` only so email tools could run
  can put `pii` back once their email tools declare `"format": "email"` on recipient fields.
- Code that imported `examples.case_study_responsible_hiring` or `examples.pattern_text_to_sql`
  from the installed package must run them from a repository checkout.
- `pip install "algen-agent-tools[smtp]"` (or `[gmail]`, `[postgres]`) and follow its
  `docs/migrating-from-runtime.md`. The transports and message models keep their names under
  `algen_agent_tools.mail`, `.smtp` and `.gmail`; the new `send_email` tools are named
  `smtp.send_email` / `gmail.send_email` with permissions `smtp.write` / `gmail.write`, take the
  sender from deployment configuration and accept no custom headers.

## [0.1.0a11] - 2026-10-27

### Added

- `domain_plugins` now loads: `build_container` imports each plugin an agent names, once, and calls
  its `register(context)`. A plugin is named as `module:attribute` or as an entry point in the
  `algen_agent_runtime.plugins` group, and must sit under `security.trusted_plugin_prefixes`;
  trust is checked for the whole set before anything is imported. The context registers tools,
  planners, context builders, verifiers, response composers, semantic layers, analytical methods,
  model services, model providers and MCP servers, and `add_resource` hands a shared client to the
  container's lifecycle. A name that is already registered, by the Runtime, the manifest or an
  earlier plugin, raises `ConflictError`.
- `Container.plugins` lists the loaded plugin manifests and `Container.component_sources` records
  which plugin registered each component.
- `PlannerRegistry`, `ResponseComposerRegistry`, `ContextBuilderRegistry` and
  `VerificationService` gain `names()`.
- `describe_components(settings)` (`algen_agent_runtime.orchestration.describe`) and
  `algen-agent-runtime describe [--config ...] [--path src] [--json]` list everything a container
  built from the settings would register, by kind and source (`runtime`, `manifest` or the plugin
  reference): tools with side effect, idempotency, permissions, timeout, attempts and schemas;
  planners, composers, verifiers, context builders; provider types and providers; MCP servers;
  semantic layers, methods and model services; and every environment variable a deploy must
  supply. It imports plugins and runs their `register`, but calls no component, resolves no secret
  and connects to nothing.
- `PluginManifest.env` declares the environment variable names a plugin's components read.
- `build_container` checks every name an agent uses (`enabled_tools`, `planning_strategy`,
  `response_composer`, `verification_policy.verifiers`, `context_builder`, model providers) against
  what was registered and fails with a `ConfigurationError` that lists each unknown name with the
  closest registered one. Enabled MCP tools are checked when `Container.astart()` connects to their
  server. `runtime.component_check: warn` logs instead of failing.
  `algen_agent_runtime.orchestration.validation` runs the same check against a `describe` inventory.
- `ModelRouter.registration_ids()`.
- `algen-agent-runtime check [agent.yaml] [--path src] [--project-root .] [--json] [--strict]` and
  `check_project` (`algen_agent_runtime.orchestration.check`) validate a project as Studio will:
  settings, plugins, conflicts, unknown names with the closest match, and provider types are errors;
  a static scan of the project's Python warns about its own container, constructor components,
  registration on a built container, direct `os.environ` reads and agents defined in code. The JSON
  report carries the full inventory.
- `load_settings(..., ignore_extensions=True)` drops a top-level `extensions` mapping instead of
  rejecting it.
- Tool authoring (`algen_agent_runtime.tools.authoring`): `@tool` turns a typed function into a
  `Tool`, with strict input and output schemas from type hints or Pydantic models and the
  description from the docstring; a `ToolContext` parameter receives the call's context. Presets
  `read()`, `write(approval=...)`, `external(approval=...)`, `destructive()` and
  `long_running(...)` set side effect, permission, idempotency, retries and timeout together
  (writes are keyed and single-attempt) and can be overridden with `.replace(...)`. `Toolkit`
  groups tools that share a lazily opened client, `env://` secrets and a permission prefix, and
  registers through a plugin. `ApprovalHandler` lets a deployment run its own approval flow around
  a tool call. See `docs/tool-authoring.md`.
- `ToolDefinition.requires_approval` asks for the Runtime's approval before every call of a tool,
  whatever the agent's approval policy; `describe` reports it.
- `algen_agent_runtime.tools.testing`: `fake_tool_context`, `assert_tool_contract` and
  `tool_contract_problems`.
- `examples/quickstart_agent_tool`: a toolkit with a read tool and an approval-gated write tool,
  declared through `domain_plugins`, run offline.
- The plugin context gains `secrets` (resolves `env://` with the container's environment) and
  `declare_env(...)`; `MockModelProvider.queue(...)` scripts its next replies.
- `algen-agent-runtime new` (the `agent` template) scaffolds `<name>_plugin.py` with a toolkit and
  its `domain_plugins` entry, and the generated project passes `check`.
- `docs/studio-compatibility.md` lists what Studio can and cannot see; `docs/plugins.md` has the
  steps for moving from constructor arguments to a plugin.
- Plugins register guardrail policies (`context.policies`), the handler for an analytical node kind
  that has none (`context.analytical_nodes`, for `semantic_query` and `model_call`), and search stores
  with their `retrieval.<name>` context builder (`context.retrievers`); `describe` lists all three.
- The startup check covers `guardrail_policy.policies` and `guardrail_policy.tool_policies`. The
  policy engine skips a name it does not know, so an unregistered or misspelt guardrail used to be
  silently off; it now fails the build. `check` also flags policies, search stores, semantic layers
  and analytical handlers registered on a built container.
- A `documents` extra installs PyMuPDF for PDF inspection.
- Whole-run trace export: with Traccia, a `RunScopedExportProcessor` holds each run's spans until its
  root ends and exports them together, so a backend never receives part of a run (a lost middle
  batch, or child spans whose root never arrives). `telemetry.traccia.export_whole_runs` (default
  on), `run_export_max_spans` and `run_export_max_seconds` bound it; `install_run_scoped_export`
  serves other OpenTelemetry deployments. The root records `agent.run.span_count`.
- A run resumed after an approval or a pause continues its first trace: `RunState.trace_parent`
  stores the first `agent.run` span and the resumed `agent.run` is its child.
- `ObservabilityAdapter.tracer(name)`; `ToolExecutor(..., tracer=...)`.
- Run narrative: `GET /v1/runs/{run_id}/narrative` and `AgentRuntime.narrative(run_id, tenant_id)`
  tell a run in order from its state and events (model calls and requested tools, tool calls with
  argument keys and result sizes, approvals, policies that acted, verification, outcome, totals and
  `trace_id`), with no prompt, answer or tool content.
- Safe-by-default span signals: `llm.response.tool_names`, `llm.response.output_chars`,
  `llm.request.message_count` and `llm.request.tool_count` on model calls; `tool.argument_keys`,
  `tool.result_chars` and `gen_ai.tool.name` on tool calls; `retrieval.scores` on context builds;
  input and output sizes and tokens on the run root. Timing: `llm.time_to_first_token_ms` on
  streamed calls and `agent.model.prepare_ms` before every model call.
- `TokenUsage.cache_write_tokens`; the Anthropic adapter reports tokens written to its prompt cache.
- `docs/plugins.md` documents plugins and the inventory; the agent-builder, connector, extension and
  contributor skills now register code components from a declared plugin instead of
  `build_container(additional_tools=...)`.

### Changed

- `additional_tools=` and `mcp_servers=` are merged after declared plugins; they keep working
  as before.
- Trusted plugin prefixes match on module boundaries: `acme` trusts `acme.crm` but no longer
  `acme_evil`.
- An agent that declares `domain_plugins` may name a model provider not listed under `providers:`,
  since a plugin may register it; the startup check fails if none did.

- `examples/pattern_text_to_sql` (and its `advanced` variant) and
  `examples/case_study_teaching_assistant` register their tools from a declared plugin instead of
  on the built container, and every example project passes `check` in the test suite. The
  text-to-SQL database connects on first query.
- A plugin module that cannot be found says to install its package or pass `--path`.
- With Traccia enabled, every Runtime span is created through Traccia's tracer, so its enrichment
  processors see model, tool, step and memory spans too. Before, only the run root and guardrail
  spans went through them: the root's `guardrail.summary` reported "No LLM or tool spans observed"
  and agent identity was missing from the other spans.
- Every Runtime span has a `span.type` (`step`, `planning`, `context`, `verification`, `memory` and
  `cache` are new), and the tool executor's policy spans follow `telemetry.trace_level`.
- `telemetry.trace_level: minimal` now keeps the decision chain: steps, one policy summary per
  checkpoint and verification join the run, model and tool spans. Context building and memory writes
  move to `standard`; planning, cache and unfired guardrails stay at `detailed`.
- Model-span tokens follow Traccia's pricing and OpenTelemetry's conventions without duplicates:
  `llm.usage.prompt_tokens` is now the uncached input, with `llm.usage.cache_read_tokens` and
  `llm.usage.cache_write_tokens` beside it, and `gen_ai.usage.input_tokens` stays the whole prompt.
  `llm.usage.input_tokens`, `llm.usage.output_tokens`, `llm.usage.cached_tokens` and the per-direction
  `*_source` attributes are gone.
- An unpriced model call (no provider rates) carries no `llm.cost.usd` instead of 0.0, so Traccia
  prices it; the run root carries `agent.run.estimated_cost_usd` only when priced. Before, the 0.0
  stopped Traccia's cost processor from pricing Runtime model calls at all.
- Descriptive agent attributes (description, tags, capabilities, planning strategy, run mode) are on
  the run root only, and empty values are left out.
- `examples/case_study_teaching_assistant` registers its education guardrail policies from its
  plugin. Before, only `dashboard.py` registered them, so `app.py` ran with them silently off.

### Removed

- The `talk-to-data` and `hiring` extras, which only bundled dependencies under product names.

### Upgrade notes

- Trace queries and dashboards: `llm.usage.prompt_tokens` now counts only uncached input; read
  `gen_ai.usage.input_tokens` for the whole prompt. `llm.usage.input_tokens` and
  `llm.usage.output_tokens` become `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens`,
  and `llm.usage.cached_tokens` becomes `llm.usage.cache_read_tokens`. Unpriced calls no longer
  carry `llm.cost.usd: 0.0`. At `trace_level: minimal`, expect step, policy-summary and
  verification spans that used to need `standard` or `detailed`.
- Replace the removed extras in each project's dependencies: `algen-agent-runtime[talk-to-data]`
  becomes `algen-agent-runtime[pgvector,traccia]`, and `algen-agent-runtime[hiring]` becomes
  `algen-agent-runtime[pgvector,traccia,documents]` plus `python-multipart` in the project itself if
  it accepts form uploads. Keep `traccia` explicitly: pip only warns about an unknown extra and
  installs nothing for it, so a project still naming the old extra loses Traccia and asyncpg.
- An agent whose `guardrail_policy` names a policy that is registered only on the built runtime
  (`container.runtime.policies.register`) now fails to build: register it from a plugin.
- An agent that names a tool, planner, composer, verifier, context builder or provider that was
  not registered by the time `build_container` returns now fails to build. A deployment that
  registers components on the built container (for example `register_starter_tools(container.tools)`
  for an enabled tool) should move them into a plugin, or pass them as `additional_tools=`; set
  `runtime.component_check: warn` meanwhile.

## [0.1.0a10] - 2026-10-26

### Changed

- The Anthropic adapter uses prompt caching by default: an explicit cache breakpoint at the end of
  the system prompt, and automatic caching for the growing conversation. An agent loop's later calls
  read the tools, instructions and earlier turns from the cache (about a tenth of the input price)
  instead of paying for them again. A gateway that rejects `cache_control` is configured with
  `prompt_caching: false` on its provider entry (or `prompt_caching=False` on `AnthropicProvider`).
- Anthropic usage reports the whole prompt as `input_tokens` (uncached, written to the cache and
  read from it; Anthropic's own `input_tokens` is only the uncached part), and the part read from
  the cache as `cached_tokens`, as the OpenAI adapters do. Token budgets and metering that read
  `input_tokens` stay correct with caching on.

## [0.1.0a9] - 2026-10-24

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
- The `openai-agents` and `frameworks` extras require `openai-agents>=0.21`, the first release with
  the `agents.testing` helpers the adapter contract tests use. Without the floor, resolvers could
  fall back to 0.20 when another framework constrained `openai` or `pydantic`.

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

[Unreleased]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a18...HEAD
[0.1.0a18]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a17...v0.1.0a18
[0.1.0a17]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a16...v0.1.0a17
[0.1.0a16]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a15...v0.1.0a16
[0.1.0a15]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a14...v0.1.0a15
[0.1.0a14]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a13...v0.1.0a14
[0.1.0a13]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a12...v0.1.0a13
[0.1.0a12]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a11...v0.1.0a12
[0.1.0a11]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a10...v0.1.0a11
[0.1.0a10]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a9...v0.1.0a10
[0.1.0a9]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a8...v0.1.0a9
[0.1.0a8]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a7...v0.1.0a8
[0.1.0a7]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a6...v0.1.0a7
[0.1.0a6]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a5...v0.1.0a6
[0.1.0a5]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a4...v0.1.0a5
[0.1.0a4]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a3...v0.1.0a4
[0.1.0a3]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a2...v0.1.0a3
[0.1.0a2]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a1...v0.1.0a2
[0.1.0a1]: https://github.com/AlgenAI/algen-agent-runtime/releases/tag/v0.1.0a1

# Upgrading

Owner: AlgenAI Maintainers  
Status: Active (`0.2` beta line)  
Last reviewed: 2026-11-15

This guide takes a deployment, an agent project or a host application from the `0.1` line to
`0.2`. It collects the upgrade notes of each `0.2` release ([CHANGELOG.md](../CHANGELOG.md)); the
changelog stays the full record, with every fix and addition. The `0.1` alphas' own notes are in
[CHANGELOG-0.1.md](../CHANGELOG-0.1.md).

A guide like this one is kept for each `0.x` minor release up to `1.0`
([GA criteria](ga-criteria.md#upgrades-and-data)).

## Before you start

1. **Get to `0.1.0a23` first.** The `0.2` notes assume the last `0.1` release. From an earlier
   alpha, read the "Upgrade notes" of each alpha after yours in
   [CHANGELOG-0.1.md](../CHANGELOG-0.1.md). The ones that change a running deployment most are:
   - `0.1.0a18`: migrations `015` and `016` (016 locks the artifact, memory and learned-memory
     tables until its backfill commits; run it in a quiet period), store outages answer 503 with
     `Retry-After`, PostgreSQL and Redis command timeouts, credentials in tool arguments are
     refused, `core.http` refuses private addresses unless allowlisted;
   - `0.1.0a19`: `ToolExecutor.execute` enforces `requires_approval`, and a workflow `tool:` step
     on such a tool needs an approval node before it;
   - `0.1.0a22`: synchronous tools run off the event loop and must be thread-safe; two workflow
     nodes may not share an `output_key`.
2. **Take a backup** of the Runtime's PostgreSQL database (see
   [Backup and restore](operations.md#backup-and-restore)).
3. **Remove what `0.2.0b1` removed** while still on `0.1.0a23`, where the deprecated interfaces
   still warn: see [Removed interfaces](#removed-interfaces).
4. **Upgrade one process per database.** The GA scope is a single Runtime process per deployment
   ([Deployment scope](production-readiness.md#deployment-scope)); recovery
   (`recover_incomplete_runs`) runs on exactly one process per database.

Migrations are forward-only and additive and run on startup under an advisory lock. A host that
owns the schema applies them itself: `algen-agent-runtime migrations sql --since NNN` prints the SQL
of each migration after `NNN` (from `0.2.0b3`), and `check_schema(...)` verifies the result
([Hosts that own the schema](operations.md#hosts-that-own-the-schema-migrations-and-check_schema)).

| From | Migrations to apply | Long locks |
| --- | --- | --- |
| `0.1.0a23` | `017` to `023` | `021` and `022` build indexes on the runs table: on a large table create them `CONCURRENTLY` first (below) |
| `0.2.0b1` | `019` to `023` | as above |
| `0.2.0b2` | `021` to `023` | as above |
| `0.2.0b3` | `023` (see [0.2.0b4](#from-020b3-to-020b4)) | none: one nullable column and a partial index on the artifacts table |

Each migration, and whether it can stay in place on a rollback, is listed under
[Upgrades and rollback](operations.md#upgrades-and-rollback).

## From `0.1.0a23` to `0.2.0b1`

### Removed interfaces

| Removed | Use instead |
| --- | --- |
| `algen_agent_runtime.communications` | `algen_agent_tools.mail` and the SMTP or Gmail toolkit ([email](email.md)) |
| `tools.adapters.database_tool` | `PostgresToolkit.query` from `algen-agent-tools` |
| Document extractors in `algen_agent_runtime.security` (`documents`, `docx`, `pptx`, `xlsx`, `html`, `ooxml`) and the `documents` extra | The same names from `algen_agent_tools.documents.inspection`, with `algen-agent-tools[documents]` ([document security](document-security.md)) |
| A `UsageMeter.reserve()` without a `call` parameter (the `0.1.0a14` form) | `reserve(..., call: MeterCall)`; the old form is refused with `ConfigurationError` when the router is built |
| `ToolExecutor.execute(approval="<id>")` | `approval=ToolApproval.for_call(approval_id, "approved", tool, version, arguments)` |

### Packaging

- **Install the server explicitly.** `fastapi`, `uvicorn` and the OTLP exporter are extras:
  `pip install 'algen-agent-runtime[service]'` for a deployable service (`server` and `otlp` on
  their own). Without them `algen-agent-runtime serve` exits with the install command.
- **The examples package is `algen_agent_runtime_examples`.** Rename
  `python -m examples.<name>.app` and any `examples.<name>...` hook provider or plugin reference in
  configuration. There is no shim.

### Approvals bind the exact call

- A workflow's approval-required tool step runs only with the arguments its approval showed (or
  the approver's modification); otherwise it is refused with `workflow.tool.refused`. Template the
  step's arguments from what the approval shows, or move the approval after the nodes that produce
  them. `binding: step` keeps the old behaviour and is discouraged; `check` reports
  `APPROVAL_ARGUMENTS_UNBOUND` as an error.
- If an approved tool's definition or code changes between the request and the decision, the
  approval expires (`status_reason: tool_changed`) and the run asks again
  (`replaces_approval_id`). Clients must be ready to show a second request.
- A custom `ApprovalService` adds `expire()` and accepts the new `create()` keywords (`tool`,
  `tool_version`, `tool_fingerprint`, `arguments_sha256`, `rationale`).

### Policies, attachments and context

- The built-in secrets policy answers `redact`, not `transform`; `plan_tool` refuses `redact` and
  `require_clarification`; `transform` at `plan_tool` or `before_tool` rewrites the arguments.
  See [Actions per boundary](operations.md#actions-per-boundary).
- `attachments.types` lists only types a model reads; move `text/csv`, `image/heic` and similar to
  `tool_read_types` ([attachments](attachments.md)).
- With `shorten_older_tool_results`, older tool exchanges are kept shortened; a stray tool result
  is dropped with a warning instead of raising `ConflictError`.

### Rolling back to `0.1.0a23`

Migrations `017` and `018` can stay in place. A workflow checkpoint with `approved_calls`, or a run
state with `tool_approvals`, cannot be read by `0.1.0a23`: finish or cancel those before rolling
back.

## From `0.2.0b1` to `0.2.0b2`

Migrations `019` (session owners) and `020` (who decided an approval) are additive. Behaviour
changes, most of them security fixes from an independent review:

- **Sessions belong to their user.** A run naming another user's `session_id` is refused (404).
  Give each user their own session ids.
- **Who may act on a run over HTTP.** Only the run's user decides its approval, cancels, steers or
  answers it; approvers working from `/v1/approvals` need `approvals:decide`, operators who cancel
  other users' runs need `runs:admin`. Reads stay tenant-wide with `runs:read`.
- **`core.subagent` needs an allowlist** (`delegates` on the agent; empty allows none) and cannot
  delegate to an agent with wider `tool_permissions`; the nesting depth is counted on the server.
- **`serve --host`** on a non-loopback address refuses development-header auth unless
  `allow_insecure_development_auth` is set.
- **A failed run has no output**: `RunResult.output` is `None` unless the run completed; a
  verification failure keeps the rejected answer in `rejected_output`.
- **Structured output and guardrails**: with a `response_schema`, `after_model` and
  `final_response` policies receive the parsed value, not a string.
- **Streams end on a pause** (`approval.required`, `clarification.required`,
  `tool.reconciliation_required`); follow a resumed run with `stream_events(run_id, after=...)` or
  `Last-Event-ID`.
- **Fewer false redactions**: Aadhaar numbers need a valid Verhoeff check digit, 10-digit numbers
  need phone context to be masked as phone numbers. Deployments that relied on bare 10-digit
  numbers being masked should name the field or add their own DLP policy.
- **Recovery is single-node**: enable `recover_incomplete_runs` on exactly one process per
  database.
- **Approvals record who decided**: a custom `ApprovalService.decide` should accept
  `*, decided_by=None, comment=None`.
- **`core.subagent` from settings**: do not also register `subagent_tool(runtime)` yourself when
  `runtime.subagent.enabled: true`.

## From `0.2.0b2` to `0.2.0b3`

Migrations `021` and `022` add indexes on `algen_agent_runtime_runs`. On a large table, create
`algen_agent_runtime_runs_user_idx` and `algen_agent_runtime_runs_turn_idx` with
`CREATE INDEX CONCURRENTLY` (same definitions as in the migration files) before upgrading; the
migrations then find them and take no long write lock.

- **Workflow approval and input routes follow the run actor rule.** `POST
  /v1/workflows/runs/{id}/approval` accepts the run's user or `approvals:decide`; `/input` accepts
  only the run's user.
- **Conversation turns survive restarts.** `ConversationService.recover()` follows or settles a
  streaming turn's run instead of failing it; `shutdown()` detaches turns instead of cancelling
  them. A failed run's turn has `metadata.error_type: "RunFailed"`.
- **A recovered run that had caller credentials waits for them** in the new paused status
  `awaiting_credentials` (`credentials.required`); resume it with `credentials=` or set
  `recovery_credentials`. Clients that switch on `RunStatus` or `MessageStatus` should handle it.
- **A delegated child's pause pauses its parent** (`core.subagent`); approval inboxes list the
  request once, under the child run.
- **An oversized tool result no longer fails the run**: it is offloaded or truncated
  (`context.tool_result_truncated`). `context_policy.oversized_tool_result: fail` keeps the old
  behaviour.
- **A failed workflow agent node keeps the child run's error type** (`VerificationError`,
  `BudgetExceededError`, ...) instead of `RuntimeError`.
- **Streams stopped part-way are charged** with an estimate; a `UsageMeter.settle` that takes
  `estimated: bool = False` is told so.
- **Deleting a conversation deletes its session memory.**
- **Slow event subscribers** read on from history, or get `EventStreamOverflowError` /
  `stream.overflow`.
- **New event types**: `credentials.required`, `context.tool_result_truncated` (run events);
  `agent.run.started`, `assistant.message.steered`, `agent.steer.delivered`,
  `agent.steer.undelivered`, `assistant.message.awaiting_credentials`, `agent.tool.started`,
  `agent.tool.completed` (conversation events). Consumers that validate against a closed list add
  them.
- **`core.ask_user` is a built-in name**; a host tool of that name raises `ConflictError`.

The changelog's "You can now remove" list for `0.2.0b3` names the host code these changes make
unnecessary.

## From `0.2.0b3` to `0.2.0b4`

`0.2.0b4` fixes the findings of a security assessment of `0.2.0b3`. Migration `023` adds a
nullable `user_id` column and a partial index to `algen_agent_runtime_artifacts`; hosts that own
the schema apply it with `algen-agent-runtime migrations sql --since 22`. Check these before you
upgrade:

- **Artifacts belong to a user.** Grant `artifacts:admin` to operator tools that browse every
  file; hosts that store artifacts themselves set `user_id`. Rows from before the upgrade fall
  back to their run's user.
- **`POST /v1/agents` needs `agents:admin`**, and a version of a name another owner registered is
  refused (409). Runs keep the version they started with.
- **Approval decisions are `approved`, `rejected` or `modified` only.** Approval inboxes that
  match the literal `"[REDACTED]"` now see `[REDACTED len=N sha256=xxxxxxxx]`.
- **Streamed text is redacted and can be retracted.** Handle `model.delta.retracted` (run events)
  and `assistant.message.delta.retracted` (conversation events): discard the text shown so far.
- **Delegation.** A coordinator enables every tool its delegates enable. Delegates inherit the
  parent's approvals and guardrails, so they may pause where they did not.
- **Egress and execution.** Set `security.allowed_http_hosts` (or
  `security.allow_any_public_host: true`) for `core.http`, `security.sandbox.allowed_executables`
  for `core.subprocess`, and an explicit `api_key` for Gemini, xAI, Groq or Mistral with another
  `base_url`.
- **MCP OAuth.** Hosts that start flows from their backend set `success_redirect_url` and call
  `POST /v1/mcp/oauth/complete`; a server with `client_secret_ref` sets `issuer`.
- **Secrets in configuration.** Credential-named stdio MCP `env` values and retrieval
  `connection_url` values with a password must be `env://` references.
- **Agent UI under `jwt`/`oidc`** needs a token with `agents:read`.
- **Clarification and `core.ask_user` answers** pass the run's `input` policies; a denied answer
  raises `PolicyDeniedError` and the run stays paused.
- **API limits.** At most `api.max_streams_per_principal` (default 32) open event streams per
  principal and process (429 beyond); a non-numeric `Last-Event-ID` is a 400; CORS `*` together
  with credentials is refused at startup; a `*` scope in a static `jwt` token is ignored.
- **Plugins.** Trust prefixes are checked against the module an entry point loads; an entry point
  name declared by two distributions is refused; a signed plugin with loose `.pyc` files or a
  mismatched bytecode cache is refused (delete `__pycache__` and reinstall).
- **Generated SQL validation is strict.** `SemanticLayer.validate_generated_sql` refuses comments,
  several statements, set operations, a top-level `OR` in `WHERE`, and required filters that are
  not their own conjunct.
- **Policies.** Mandatory policies always fail closed. The secrets guardrail recognises more token
  shapes, so a tool call whose arguments it redacts may now be refused.
- **Caches and stores.** The `tool_results` cache is user-scoped unless its `scope` is set;
  PostgreSQL, Chroma, FAISS and Qdrant vector stores refuse documents without `tenant_id`.
- **Framework adapters** return `FAILED`/`CANCELLED` instead of raising; LangGraph threads are
  `"{tenant}:{session}"` (checkpoints under a bare session id are not found); pass AutoGen a
  `factory=`; give `GovernedToolBridge` a permissive `approval_policy` if a framework must call
  state-changing tools.

The full list, with the "You can now remove" entries, is in the changelog's `0.2.0b4` section.

## From `0.2.0b4` to `0.2.0b5`

`0.2.0b5` adds evidence (provider cancellation, redacted telemetry, live provider contracts),
run-level metrics with dashboards and alert rules, and fixes. It has no new required setting.
Check these before you upgrade:

- **Migration `024`** drops the tool-execution ledger's foreign key to runs, so workflow
  `tool:<name>` steps work on PostgreSQL (they failed with `ForeignKeyViolationError`). Hosts that
  own the schema apply it with `algen-agent-runtime migrations sql --since 23`; rolling back to
  `0.2.0b4` keeps working on the migrated database.
- **Provider `max_concurrency`** is enforced when you set it (it was never read); left at its
  default it sets no cap.

- **Provider `base_url`.** `openai`, `anthropic`, `deepseek` and `huggingface_inference` now call
  the configured `base_url` (they ignored it before); another host than the vendor's needs an
  explicit `api_key`. `azure_openai` needs `base_url` at build time.
- **Cancelled waiters.** Code that cancels a task awaiting `Runtime.run` or `Runtime.wait` gets
  `CancelledError` instead of a cancelled `RunResult`; an `asyncio.timeout()` around them raises
  `TimeoutError`. Use `Runtime.cancel(run_id, tenant_id)` to cancel the run itself.
- **Masked text.** Run errors, tool errors shown to the model, approver comments, conversation
  titles, steering text for a paused run, artifact metadata, API error bodies and readiness
  reasons can carry `[REDACTED…]` markers. Do not parse secrets or personal data out of them.
- **Logging.** Under your own structlog or stdlib configuration, Runtime log events arrive masked
  with an `exception` string instead of `exc_info`; add `redact_log_event` after
  `format_exc_info` to keep exception objects.
- **Metrics.** If you scrape Prometheus, the `job` label becomes `telemetry.service_name` (unless
  `OTEL_SERVICE_NAME` is set). Import the dashboards and alert rules from `deploy/observability`.
- **Experimental interfaces.** Horizontal scaling, the AutoGen and CrewAI adapters, the
  analytical graph engine, `model_services` and `decisions` are marked experimental (outside the
  1.x promise); check `is_experimental()` before building on them.

The full list is in the changelog's `0.2.0b5` section.

## Rolling back

Stop the new release and start the previous one on the same database, leaving the newer
migrations in place: every migration so far is additive, and the previous release ignores
migration rows, columns and tables it does not know. The persistence drills prove this on every
change for two previous releases, `0.1.0a23` and `0.2.0b3`
([Upgrades and rollback](operations.md#upgrades-and-rollback)). Before rolling back to `0.1.0a23`,
finish or cancel the runs and workflow runs that `0.2` approvals bind (above). Restore from the
backup instead only when a release's notes say a migration is not backward compatible; none is so
far.

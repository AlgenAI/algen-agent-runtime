# Changelog

All notable changes to Algen Agent Runtime will be documented in this file.
The project follows [Semantic Versioning](https://semver.org/) while public
contracts are explicitly marked experimental during the `0.x` series.

Each release lists, in this order and only when it has some: `### Upgrade notes` (what to change
before upgrading), `### You can now remove` (host or agent code this release makes unnecessary:
what to delete and the Runtime API that replaces it), `### Security`, `### Fixed`, `### Added`,
`### Changed` and `### Documentation`. Capabilities from releases before 0.2.0b3 that replace
host code are listed in the `algen-runtime-host` skill
(`.agents/skills/algen-runtime-host/references/host-capabilities.md`).

## [Unreleased]

### Fixed

- A credential-named argument whose value holds no credential, a boolean or null (a flag such as
  `rotate_approval_secret: true`) or exactly a secret reference (`api_key: env://OPENAI_API_KEY`,
  `secret://prod/openai`), is shown to the approver as it is instead of `[REDACTED len=…
  sha256=…]`, and an agent run no longer refuses an approval-gated call because such a value is not
  declared in the tool's input schema (`approval.undeclared_secret_parameters`, since 0.2.0b4).
  Since 0.2.0b4 this refused the Traccia azAI service's calls whose free-form request bodies carry
  API fields named like that, and Studio's assistant changes that set a provider's `api_key` or an
  MCP server's token to an `env://` reference. Any other string, and any number, under such a name
  is still hidden, and refused when undeclared. Logs and telemetry mask as before.
- Examples `reference_incident_response` and `reference_invoice_exceptions` act on the decision
  their approval node recorded (`remediation_approval`, `recommendation_approval`). They read an
  old `clarifications` list instead, so `--approve-all` recorded the approval as approved while
  the remediation was reported rejected and the invoice was held. Their plan and route nodes no
  longer look at clarifications either. A test now runs both examples approved and rejected as
  the CLI does.

## [0.2.0b5] - 2026-11-16

The last beta before 0.2.0: the remaining GA readiness evidence and the fixes it found. Provider
cancellation and redacted telemetry now have automated evidence (both found and fixed bugs);
run-level metrics, Grafana dashboards, Prometheus alert rules and published SLOs ship for the
single-node scope; experimental interfaces are marked in code; event payload shapes are part of
the checked API surface; every doc page, skill and example was reviewed. Migration 024 is additive (it drops one foreign
key). Fixes and
additive changes only: read the upgrade notes for the few visible behaviour corrections.

### Upgrade notes

1. **Migration 024 (workflow tool steps on PostgreSQL).** Every workflow `tool:<name>` step failed
   on PostgreSQL with `ForeignKeyViolationError`: its ledger record's `run_id` is the workflow
   run's id, which the ledger's foreign key to `algen_agent_runtime_runs` refused.
   `024_workflow_tool_step_ledger.sql` drops that foreign key; workflow checkpoint retention now
   deletes a workflow run's ledger records and keeps workflows with unresolved calls. Hosts that
   own the schema apply it with `algen-agent-runtime migrations sql --since 23`. Rolling back to
   0.2.0b4 keeps working on the migrated database.
2. **The service image installs the `auth` and `redis` extras**, so `auth_mode: jwt`/`oidc` and
   Redis stores work in the image built from the repository's Dockerfile
   (`requirements/service.txt` re-locked; five packages added, no other pin changed).
3. **Provider `base_url` is honoured.** `openai`, `anthropic`, `deepseek` and
   `huggingface_inference` ignored `base_url` and always called the vendor, so a configuration
   that named a gateway still sent its requests and the default key to the vendor. They now call
   the configured `base_url`; like the hosted vendors since 0.2.0b4, a `base_url` other than the
   vendor's needs an explicit `api_key`, or the container refuses to build ("set api_key
   explicitly"). `azure_openai` without `base_url` now fails at build time instead of on the
   first call.
4. **A cancelled waiter raises `CancelledError`.** A task awaiting `Runtime.run` or
   `Runtime.wait` that is itself cancelled now raises `CancelledError`, as asyncio expects,
   instead of returning the cancelled run's result. `asyncio.timeout()` around `runtime.run`
   raises `TimeoutError`; a cancelled parent no longer records its `core.subagent` call as
   completed after `run.cancelled`.
5. **More text is masked.** These can now contain `[REDACTED…]` markers: run error text and
   model-facing tool error text (personal identifiers too, honouring
   `guardrail_policy.pii.exempt_entities`), approver comments, conversation titles, steering text
   queued for a paused run, `x-artifact-metadata` credentials, API error bodies, readiness reasons
   and `degraded_dependencies`, work-queue error rows, and the exception event text and status
   description on spans. An MCP tool masks credentials its server echoes back, and a raised MCP
   error reads `MCP tool X execution error (<Type>): <masked>` without chaining the server's
   exception.
6. **The Runtime's loggers protect themselves.** Under a host's own structlog or stdlib logging
   (without `configure_logging`), Runtime log events are masked and carry a masked `exception`
   string instead of `exc_info`. Add `redact_log_event` after `format_exc_info` in your chain to
   get exception objects back.
7. **Metrics.** The OTLP meter and tracer providers report `telemetry.service_name`
   (`OTEL_SERVICE_NAME` still wins), so the Prometheus `job` changes from
   `unknown_service:python` to the service name. The analytics node metrics no longer carry the
   per-execution `analytics.graph.id` attribute (the span keeps it).
8. **Provider errors.** Anthropic streamed calls map HTTP errors by status as plain calls do
   (401 `authentication`, 403 `authorization`, 400/404/422 `invalid_request`, 429 `rate_limit`,
   5xx `unavailable`); before, every status but 429 was `unavailable`, so a streamed wrong key or
   unknown model fell back to the next model. Gemini's HTTP 400 `API_KEY_INVALID` is
   `authentication`. An error a provider reports inside a stream (Anthropic `error` events, an
   OpenAI-style `{"error": ...}` chunk) now fails the call instead of completing it with the
   partial text: a run retracts the streamed text (`model.delta.retracted`, reason `model_error`)
   and retries when the error is retryable. DeepSeek structured-output requests use JSON mode
   (`{"type": "json_object"}` plus a system message with the schema). Azure OpenAI retrieval or
   memory without an `embedding_model` needs `providers.<name>.embedding_deployment`.
9. **Provider `max_concurrency` is enforced when set.** `providers.<name>.max_concurrency` was
   documented but never read. Set explicitly, it now caps the calls in flight on that provider
   (plain calls and open streams); left at its default it sets no cap, as before.
10. **Experimental interfaces.** Marked experimental and outside the 1.x compatibility promise:
   horizontal scaling (`distributed`, `distributed_execution`,
   `maintenance.work_item_retention_days`), the AutoGen and CrewAI adapters, the analytical graph
   engine (`AnalyticalGraphEngine`, graph types and stores, `analytical_execution`,
   `maintenance.analytical_graph_retention_days`, plugin `analytical_nodes`,
   `SemanticQueryGraphPlanner`, `SemanticQueryTask`), remote model services (`model_services`)
   and the Jev decision client (`decisions`). Nothing changes at run time and nothing warns.
   Analytical methods, `AnalyticalResult`, the semantic layer and query governance stay stable.

### You can now remove

- **Host-side counting of runs, tokens, tool calls, approvals or failures** from run events or
  spans, and hand-built Runtime dashboards and alerts: use the Runtime's metrics
  (`algen_agent_runtime.observability.metrics`) and `deploy/observability`.
- **Host scrubbing of Runtime spans, exceptions, run errors and Runtime logger output**: the
  Runtime masks them in every sink, also under the host's own logging configuration.
- **Hand-written lists of supported versus experimental adapters or settings**: use
  `algen_agent_runtime.stability.is_experimental()` or `"stability": "experimental"` in the
  settings schema.

### Fixed

- A cancelled tool call no longer leaves its ledger record `started`. A call cancelled while the
  record was being written, while it waited for a `max_concurrency` slot or while the result cache
  was read is recorded `failed` (nothing ran); a call cancelled again while its outcome was being
  recorded (a turn and its run both cancelled) now finishes recording it. Such a record waited for
  an operator, and retention kept its run until then: the six-hour soak left eight after about
  7,000 cancellations.
- Cancelling a streamed model call (cancel, error or response-size limit) closes the provider's
  HTTP stream at once; `ModelRouter.stream` and `OllamaProvider.stream` left it open until the
  generator was garbage collected.
- `Runtime.cancel` and the drive it cancels no longer race: a run could end `cancelled` while
  `cancel` (so `DELETE /v1/runs/{id}`, as 409) or `wait` raised `RunConflictError`.
  `run.cancelled` is still recorded once.
- `AnthropicProvider.stream` reports a refused or dropped connection as a retryable `unavailable`
  `ProviderError`, so the router can fall back.
- Telemetry leaks found by the golden suite: an exception leaving a Runtime span (OpenTelemetry or
  Traccia) recorded its raw message and traceback; an input refused at the input boundary was
  stored raw; verifier repair requests, workflow and analytical node errors and invalid-tool-call
  audits quoted raw text; an OpenAI-compatible provider quoting the API key it was sent put it in
  the run error; declared HTTP tools returned an echoed credential; steering text, approver
  comments, conversation titles (now never cut inside an identifier), artifact metadata and
  logged tracebacks kept credentials or personal data; API error bodies, the tool-execution and
  MCP OAuth routes, readiness reasons, `degraded_dependencies`, work-queue rows and
  developer-audience conversation errors carried raw exception text.
- `runs.finished` reports `reason="cancelled"` for a cancelled run.
- Providers (found by the live contract suite): Anthropic streamed HTTP errors are mapped by
  status; mid-stream error events no longer end a stream as completed; `AzureOpenAIProvider.embed`
  posts to `/openai/deployments/<deployment>/embeddings?api-version=...` (it posted to a URL
  Azure does not serve); `embed()` has a timeout (`embed_timeout_seconds`, 60 s) and reports
  transport failures and malformed bodies as `ProviderError`; plain OpenAI-compatible calls report
  `cached_tokens`; DeepSeek's `response_schema` is sent in JSON mode, which DeepSeek accepts;
  Gemini's wrong-key 400 is an `authentication` error and list-wrapped error bodies are read.
- Found by the long soak: a run whose deadline passes while it plans, verifies or composes ends
  `timed_out` (the state machine refused the transition and left the run in its working status);
  an approved tool call costs one step of `max_steps`, not two; an interrupted read-only tool call
  runs again on replay instead of waiting for reconciliation (its `started` ledger row stayed
  forever); event streams end cleanly when the store becomes unavailable; `serve` bounds the wait
  for open connections on SIGTERM by `runtime.shutdown_grace_seconds`, so the drain runs while
  clients hold event streams (the process was killed instead); `input_template` renders
  `input_schema` properties.
- Answering a conversation turn right after it paused for approval or input no longer fails with
  "conversation already has an active response": `respond` waits (up to 5 seconds) for the turn
  task that just paused to return.

### Added

- Run-level OpenTelemetry metrics: `runs.started`/`runs.finished`, `run.duration`, `runs.active`,
  `runs.paused`, `run.pauses`/`run.resumes`/`run.recoveries`, `approvals.requested`/
  `approvals.resolved`/`approval.wait.duration` (runs and workflow approval nodes),
  `model.calls`/`model.call.duration`/`model.tokens`, `tool.calls`/`tool.call.duration`/
  `tool.refusals`, `policy.denials`, `api.requests`/`api.request.duration`/
  `api.admission.rejections`, `store.unavailable`, `postgres.pool.*`, `events.backpressure`,
  `conversation.turns`/`conversation.turn.duration`, `workflow.runs.*`/`workflow.run.duration`,
  recorded only when a meter provider is installed; the catalogue is `RUNTIME_METRICS` and
  `prometheus_name()` in `algen_agent_runtime.observability.metrics`.
- Grafana dashboards and Prometheus alert rules in `deploy/observability` (also in the sdist).
- `algen_agent_runtime.stability` (`experimental`, `is_experimental`, `experimental_reason`,
  `EXPERIMENTAL_FIELD`) marks experimental interfaces in code.
- `docs/api/surface.json` records event payload shapes (`event_payloads`) and an `experimental`
  index; `scripts/api_surface.py --changes` groups API differences as removed, changed, added and
  experimental.
- `algen_agent_runtime.security.redact_error_text`, `redact_held` and `redact_log_event`.
- `providers.<name>.embedding_deployment` (Azure OpenAI) and
  `OpenAICompatibleProvider.embed_timeout_seconds`; the `_error_kind_for`/`_http_error` hooks and
  the `stream_error`/`stream_error_kind` helpers for adapter authors.
- Live provider contracts (`tests/live`, marker `live`): a maintainer runs them before each
  release against the real APIs (`ALGEN_LIVE=1` with each provider's key) to check every
  capability each built-in provider class claims; `ALGEN_LIVE_FAKE=1` runs them offline against a
  local fake of each API ([releasing](docs/releasing.md#live-provider-contracts)).
- Examples `pattern_observability` (metrics offline, or a Collector, Prometheus and Grafana with
  the shipped dashboards and alerts through `deploy/compose.yaml`) and `pattern_run_follower` (a
  host following a streamed run: retraction, reconnect with `after=`, cancel, a timeout around
  `wait`). `pattern_secured_api` shows per-user file ownership with `artifacts:admin`;
  `quickstart_conversation` handles `assistant.message.delta.retracted`.
- Evidence suites: provider cancellation (`tests/unit/test_provider_cancellation.py`,
  `test_run_cancellation.py`, `test_tool_cancellation.py` against a local fake model API, and the
  PostgreSQL drill `tests/drills/test_cancellation.py`); golden telemetry and redaction
  (`tests/telemetry`: canaries through every run path against every sink, golden shape files,
  PostgreSQL, Redis and host-logging variants; regenerate with
  `ALGEN_UPDATE_GOLDEN=1 python -m pytest tests/telemetry`).
- Security rules enforced by tests and CI (AGENTS.md, "Security invariants"):
  `tests/unit/test_api_tenant_matrix.py` classifies every HTTP route, calls each one without an
  identity (401) and as another tenant with every scope (403 or 404, no data), so a route added
  later is checked; `tests/unit/test_approval_decisions.py` sorts every `ApprovalStatus` and runs
  the tool only for `approved` and `modified`; `tests/unit/test_tool_execution_chokepoint.py`
  fails when code outside `ToolExecutor` calls a tool's handler. `scripts/check-workflows.sh` audits
  `.github/` with zizmor (hash-locked in `requirements/security.txt`), in a new `workflows` job of
  the Security workflow and in `scripts/ci-local.sh`.
- A long soak of the service image (`scripts/long-soak.sh`, `tests/perf/long_soak/`): hours of
  mixed HTTP load (runs followed over SSE, approvals, workflows, cancellations, conversation turns,
  artifacts, a noisy tenant) on PostgreSQL and Redis, with SIGTERM and SIGKILL restarts and a
  PostgreSQL restart, rolling retention, and invariants checked at the end (nothing lost or
  duplicated, nothing stuck, every aged row deleted). Six hours at `0.2.0b5` held every invariant;
  the results are in docs/operations.md, "Long soak (hours)".

### Changed

- CI: pinned actions updated (`actions/download-artifact` v8.0.2, `actions/upload-artifact`
  v7.0.2, `actions/setup-python` v7.1.0, `actions/attest-build-provenance` v4.2.2,
  `github/codeql-action` v4.39.1; Dependabot #148–#152). Dependabot updates the CodeQL actions as
  one group. Dependabot waits 14 days before proposing a new release of a Python package or the
  base image too, as it already did for actions (security updates are not delayed).
- The service image upgrades pip to 26.3, hash-locked in the new `requirements/pip.txt`. The base
  image's pip 25.0.1 had six published CVEs (CVE-2025-8869, CVE-2026-1703, CVE-2026-3219,
  CVE-2026-6357, CVE-2026-8643, CVE-2026-13346; one low, five moderate). The Runtime never runs
  pip, but an image that extends ours with `pip install` did. The Security workflow's image scan
  again uploads to code scanning, and its report includes MEDIUM findings (the job still fails
  only on HIGH and CRITICAL), so a moderate CVE in the image is raised as an alert.
  `.trivyignore.yaml` ignores two setuptools CVEs (CVE-2025-47273, CVE-2026-59890) for
  `setuptools@70.3.0` only, until 2027-02-17: that version is pip's vendored `pkg_resources`, and
  neither flaw is in it (they are in `PackageIndex.download` and the sdist `FileList`).
- A workflow approval decision is compared with the decisions that approve (`approved`,
  `modified`) instead of with `rejected`. Behaviour is unchanged: other values were already
  refused before this point.

### Documentation

- The README is a short introduction (157 lines, from 616): what the Runtime is, the one-minute
  demo, install, a quickstart, the documentation and the AI coding skills, with absolute links so
  they also work on PyPI. Everything it held moved, unchanged, to the new
  `docs/getting-started.md`: install with extras, both offline quickstarts, the CLI, the HTTP
  service and manifest UI, the architecture diagram, the capability table, the product family,
  status, the examples and the skills, with the index of every guide.
- Every doc page, Agent Skill, example README and `docs/docs.html` reviewed for the 1.0
  single-node scope; the threat model's controls table rebuilt with the 0.2.0b4 findings;
  `docs/upgrading.md` covers migration 023 and every 0.2.0b4 note; `docs/providers.md` lists every
  provider type with its default secret, endpoint and `base_url` handling.
- Operations: published single-node SLOs, a metrics reference, dashboards, alert table and
  runbooks; "Cancellation" (what stops and what does not); "Redacted telemetry" (the guarantee per
  sink, the working-data exceptions, what a host still owns).
- The comparison in "Who should use it" refreshed on 2026-11-16 with dated sources.
- `docs/studio-compatibility.md` describes how Studio checks projects and the 0.2.0b4 settings an
  imported project declares; historical pages carry banners.
- `docs/repository-settings.md` describes the `v*` tag ruleset correctly and records the
  2026-11-16 settings audit; `docs/releasing.md` lists what the GitHub release carries.
- `AGENTS.md` gains "Security invariants": each rule from the 0.2.0b3 assessment with the test
  that enforces it, which changes need a security review, and private reporting. `CLAUDE.md`
  imports `AGENTS.md`. `docs/releasing.md` requires a security review before a new minor line or a
  release that adds a route, an authentication mode, a way to run tools or a workflow change.
- `docs/api-stability.md`: how experimental interfaces are marked and what the recorded surface
  covers. `docs/production-readiness.md` and `docs/ga-criteria.md` updated with the evidence.

## [0.2.0b4] - 2026-11-15

Fixes from a security assessment of 0.2.0b3 (the application and the release pipeline, after the
2026 LiteLLM supply-chain compromise), the security and correctness part of the framework adapter
issue ([#146](https://github.com/AlgenAI/algen-agent-runtime/issues/146)), and GA evidence:
performance baselines, load and noisy-neighbour results, upgrade and rollback drills from both
0.1.0a23 and 0.2.0b3, an upgrade guide and a support policy. Migration 023 is additive (one
nullable column and an index). Several security fixes change behaviour: read every upgrade note.
This is the last beta planned to change behaviour; later 0.2 betas take fixes and additive
changes only.

### Upgrade notes

1. **Migration 023 (artifact owner).** `023_artifact_owner.sql` adds a nullable `user_id` column
   and a partial index to `algen_agent_runtime_artifacts`. Hosts that own the schema apply it with
   `algen-agent-runtime migrations sql --since 22`.
2. **Artifacts belong to a user (security).** Uploads record the uploader and run outputs the
   run's user. List, read, download and delete are allowed to the owner, the run's user (the
   conversation owner for a turn) or a principal with the new `artifacts:admin` scope; anyone
   else gets 404. A listing without filters returns the caller's own artifacts. Runs, conversation
   messages and workflow starts refuse an attachment the caller may not use, with the same error
   as a missing one. Rows written before 0.2.0b4 have no owner and fall back to their run's user;
   rows with neither need `artifacts:admin`. Grant `artifacts:admin` to operator tools that browse
   all files. Hosts that store artifacts themselves should set `user_id`.
3. **Registering agents over HTTP needs `agents:admin` (security).** `POST /v1/agents` required
   only `agents:write`, and the registry is shared by every tenant, so a user in one tenant could
   register a higher version of another tenant's agent and take over its runs. It now needs
   `agents:admin`, records an `agent.registered` audit event, and refuses (409) a version of a
   name another owner registered: agents from configuration or the host, or another tenant's.
   Runs keep the agent version they started with when they pause, resume or are recovered.
4. **Approval decisions are approved, rejected or modified only (security).** A decision of
   `pending` or `expired` used to approve and run the tool while the record still said `pending`.
   The HTTP route answers 422, `Runtime.resume` and both approval services raise, and
   `modified_parameters` are refused unless the decision is `modified`.
5. **Approvers see a marker for hidden values.** Approval `redacted_parameters` (and the inbox's
   modified parameters) show `[REDACTED len=N sha256=xxxxxxxx]` instead of `[REDACTED]`, and
   credential-named keys are hidden at any depth. Inboxes that match the literal `"[REDACTED]"`
   need updating. An approval-gated call with a credential-named argument the tool's input schema
   does not declare is refused (`approval.undeclared_secret_parameters`), so a model cannot hide
   a payload from the approver under a key like `token`.
6. **Streamed text is redacted.** `model.delta` text now passes the after_model and final_response
   policies in windows before it is sent: the last 128 characters are held back until more text
   arrives or the answer ends, so short answers arrive as one delta. When the Runtime withholds
   or replaces streamed text it emits `model.delta.retracted` (conversations:
   `assistant.message.delta.retracted`); clients discard what they showed. Set
   `guardrail_policy.stream_text: off` to send no deltas.
7. **Delegation cannot widen what a run may do (breaking for some agent configurations).** A
   coordinator must enable every tool its delegate enables (except `core.working_state_update`,
   `core.skills_load`, `core.context_read`), or `core.subagent` refuses the delegation. A delegate
   runs with its parent's approval requirements and guardrails added, so delegated children may
   pause for approvals they skipped before. A child's usage and tool calls count against its
   parent's budget, and `runtime.subagent.max_descendants` (default 64) caps a run tree.
8. **Clarification and `core.ask_user` answers pass the run's `input` policies**, as steering
   does. A denied answer raises `PolicyDeniedError` and the run stays paused.
9. **Egress and execution defaults (security).**
   - `core.http` refuses every call while `security.allowed_http_hosts` is empty, unless the new
     `security.allow_any_public_host: true` is set.
   - `core.subprocess` runs only executables listed in the new `security.sandbox.allowed_executables`
     (absolute paths; empty refuses every call).
   - Gemini, xAI, Groq and Mistral providers with a `base_url` other than the vendor's need an
     explicit `api_key`; the default `env://<PROVIDER>_API_KEY` is no longer sent to another host.
   - HTTP tools stop paginating at a `Link: rel=next` on another origin (scheme, host or port) and
     return the pages they have with `pagination_stopped`, for every auth type. Caller-auth tools
     used to raise there.
   - Outbound connections refuse IPv6 transition addresses (6to4 `2002::/16`, Teredo
     `2001::/32`, NAT64 `64:ff9b::/96`) whatever IPv4 address they carry; a 6to4 form of a
     loopback or metadata address passed before on some Python releases.
10. **MCP OAuth (security).** The callback completes only in the browser that started the flow:
    `start` sets an HttpOnly, SameSite=Lax binding cookie, and a callback without it consumes
    nothing and redirects to `success_redirect_url` with `status=confirm` (400
    `mcp_oauth_unbound` when none is configured), where the host calls the new
    `POST /v1/mcp/oauth/complete` (`mcp:connect`, same tenant and user as the start). Direct
    `MCPOAuthManager.complete()` callers pass `browser_binding=` or `tenant_id=`/`user_id=`. A flow
    started before the upgrade finishes only through `/complete`. A server with
    `client_secret_ref` needs `issuer` (or both endpoints), so the secret never goes to an
    authorization server the MCP server advertises.
11. **Configuration that may hold secrets.** A stdio MCP server's `env` value for a
    credential-named variable must be an `env://` reference (now resolved before launch), and a
    retrieval `connection_url` that contains a password must be `env://`. Configuration errors no
    longer print the offending values.
12. **Agent UI.** Under `jwt` or `oidc` the UI page and spec need a token with `agents:read`
    (they stay open under loopback development headers). The spec no longer carries the
    `security`, `telemetry` or `cache` settings or a generated `raw_yaml`, and page values are
    HTML-escaped.
13. **API limits and checks.** A principal may hold 32 open event streams per process
    (`api.max_streams_per_principal`; 429 with `Retry-After` beyond). A non-numeric
    `Last-Event-ID` is a 400. CORS `*` together with credentials is refused at startup. A `*` scope
    in a static `jwt` token is ignored, and `check` warns `JWT_AUDIENCE_MISSING` when an
    asymmetric `jwt` setup names no audience.
14. **Plugins.** Trust prefixes are checked against the module an entry point loads, an entry
    point name declared by two distributions is refused, and a signed plugin with loose `.pyc`
    files or a bytecode cache that does not match its signed source is refused.
15. **Generated SQL validation is strict.** `SemanticLayer.validate_generated_sql` now tokenises
    the query and refuses comments, more than one statement, set operations, `OR` at the top of
    `WHERE`, and required filters that are not their own conjunct against a constant or parameter.
    Queries that passed only because a comment or a tautology mentioned the filter are refused.
16. **Policies.** Mandatory policies always fail closed, whatever `on_error` or
    `boundary_on_error` say. The secrets guardrail recognises more (token shapes such as `sk-`,
    `AKIA`, `ghp_`, `xox?-`, JWTs, DSN passwords, PEM keys and quoted JSON values), so a tool call
    whose arguments it redacts is refused where 0.2.0b3 let it through. Detectors match on
    Unicode-folded text too (NFKC, format characters removed, Cyrillic and Greek lookalikes).
17. **Caches and stores.** The `tool_results` cache is user-scoped unless its `scope` is set, and
    tools of MCP servers with per-user OAuth always key by user. PostgreSQL, Chroma, FAISS and
    Qdrant vector stores refuse documents without `tenant_id`. The in-memory tool ledger refuses
    another tenant's idempotency key.
18. **Framework adapters (#146).**
    - `invoke` returns `FAILED` (with a redacted `metadata["error"]`) or `CANCELLED` instead of
      raising the framework's exception.
    - LangGraph runs on thread `"{tenant}:{session}"`; checkpoints written under a bare
      `session_id` are no longer found, and a caller-supplied `thread_id` or identity is refused.
      An interrupted graph reports `AWAITING_INPUT` (it reported `COMPLETED`), with
      `metadata["interrupts"]`.
    - OpenAI Agents no longer sends `conversation_id` by default (pass the new `conversation_id=`
      callable), and `openai_agents_options` may not set `context`, `conversation_id`,
      `previous_response_id`, `session` or `run_config`.
    - AutoGen takes a per-request `factory=` (preferred); a shared team is reset before each run
      and runs one request at a time. AutoGen reports `persistence` and `human_in_the_loop` as
      false; CrewAI reports `streaming` false and `cancellation` only with native `akickoff`.
    - `GovernedToolBridge` refuses `write`, `external` and `destructive` tools under its default
      `ApprovalPolicy()`; pass a permissive `approval_policy` to allow them.

### You can now remove

- **Host-side re-redaction of streamed deltas** before showing them: the Runtime redacts
  `model.delta` text and retracts it when the answer changes.
- **Per-user filtering of artifact listings and downloads** in front of `/v1/artifacts`: the
  routes check the owner.
- **Token-shape and DSN-password regexes kept next to `redact_secrets`**: the shared rules cover
  them; `is_sensitive_key` and `redact_secret_text` are public.
- **Host checks that refuse approval decisions other than approve, reject or modify**: the
  Runtime refuses them.
- **Locks or per-tenant team instances around a shared AutoGen team**: pass `factory=` to
  `AutoGenAdapter`.

### Security

- Application findings fixed (Critical: cross-tenant agent takeover through `POST /v1/agents`;
  High: `pending`/`expired` approval decisions approving calls, framework tool bridge running
  side-effecting tools unapproved, streamed text skipping output policies, framework adapters
  sharing state across tenants and reporting interrupted runs as completed, the unauthenticated
  agent UI exposing configuration, artifacts readable by every user of a tenant, plugin trust
  checked on a different package than the one imported, generated-SQL validation bypassable with
  comments or tautologies; and the Medium and Low findings listed in the upgrade notes).
- **Release pipeline.** Every GitHub Action is pinned to a full commit SHA; Trivy and Gitleaks
  run as checksum-verified binaries (the `trivy-action` tag move was the entry point of the 2026
  LiteLLM compromise). No checkout keeps its token. The release workflow builds in a job of its
  own with only the hash-locked `requirements/build.txt`, restores no cache, attests the files it
  uploads (GitHub provenance, plus PEP 740 on PyPI) and attaches `SHA256SUMS`, a CycloneDX SBOM
  and a license report to the GitHub release. Auto Release starts it with `GITHUB_TOKEN`, so no
  personal access token is needed. `tests/unit/test_workflow_supply_chain.py` keeps these rules.
- **Locked dependencies.** CI, the release build, the scanners and the service image install
  hash-locked requirements (`requirements/*.txt`, `scripts/lock-requirements.sh`); the image's
  base is pinned by digest.

### Added

- Scopes `artifacts:admin` and `agents:admin`; settings `api.max_streams_per_principal`,
  `runtime.subagent.max_descendants`, `security.allow_any_public_host`,
  `security.sandbox.allowed_executables`, `guardrail_policy.stream_text`; event
  `model.delta.retracted`; route `POST /v1/mcp/oauth/complete`; check code
  `JWT_AUDIENCE_MISSING`.
- `algen_agent_runtime.artifacts`: `ARTIFACTS_ADMIN_SCOPE`, `artifact_owned_by`,
  `list_user_artifacts`; `user_id` on `Artifact` and `ArtifactDescriptor`; optional
  `list_for_user` on artifact stores.
- `algen_agent_runtime.security`: `is_sensitive_key`, `redact_secret_text`,
  `SECRET_TOKEN_PATTERNS`, `hidden_value_marker`, `undeclared_sensitive_parameters`,
  `fold_text`.
- `AutoGenAdapter(factory=, input_factory=, handoff_target=)`, `LangGraphAdapter.thread_id`,
  `OpenAIAgentsAdapter(conversation_id=)`, `GovernedToolBridge(approval_policy=, policies=)`,
  `MCPOAuthManager.server_for_state`.
- `scripts/container-smoke.sh` (start, readiness, one run, drain) in CI and `ci-local.sh`;
  `scripts/sbom.py`; a `perf` test suite (`tests/perf`, run with `-m perf`) for step and tool-call
  overhead, load and noisy-neighbour behaviour.

### Documentation

- `docs/upgrading.md` (0.1 to 0.2), the support and deprecation policy
  (`docs/api-stability.md`), the single-node GA scope and framework adapter support decision,
  measured performance and capacity evidence with proposed SLOs, alerts and runbooks
  (`docs/operations.md`), release verification and dependency locks (`docs/releasing.md`), the
  repository settings audit (`docs/repository-settings.md`), and stale version references
  removed across `docs/*.md`.
- The upgrade and rollback drills run from both 0.1.0a23 and 0.2.0b3.

## [0.2.0b3] - 2026-11-12

Conversation turns survive restarts and deploys, pauses can be followed end to end (a delegate's
approval or question surfaces on its parent, the agent can ask the user, a recovered run waits for
the caller's credentials), a run at a limit can answer instead of failing, hosts that own the
Runtime's PostgreSQL schema get its migration SQL and a schema check, one user's data can be
exported or erased, and the Runtime's Agent Skills install into a project. Migrations 021 and 022
are additive (indexes only). The public surface only grows (`docs/api/surface.json`); some
behaviour changes: read the upgrade notes.

### Upgrade notes

1. **Migrations 021 and 022 (indexes).** `021_user_data_indexes.sql` adds an expression index on
   runs by user and an index on session owners by user (user data export and erasure);
   `022_run_turn_index.sql` adds a partial expression index on runs by tenant, conversation and
   turn (finding a turn's run after a restart). Both are additive, but on a large
   `algen_agent_runtime_runs` table each index build holds a write lock on the table while it
   runs. Create `algen_agent_runtime_runs_user_idx` and `algen_agent_runtime_runs_turn_idx` with
   `CREATE INDEX CONCURRENTLY` (same definitions, in the files) before upgrading, or apply them in a
   quiet period; the migrations then find them. Hosts that own the schema apply both with
   `algen-agent-runtime migrations sql --since 20`.
2. **Workflow approvals follow the run actor rule (security).** `POST
   /v1/workflows/runs/{id}/approval` now accepts only the user who started the workflow run, or a
   principal with `approvals:decide` (with `runs:write`, as before); others get 403. Callers that
   decide other users' workflow approvals, such as a service token or an approver UI, need
   `approvals:decide`. The decision's `decided_by` is the authenticated user, as before. `POST
   /v1/workflows/runs/{id}/input` now accepts only the user who started the workflow run, as an
   agent run's `/clarification` does; no scope lets another user answer, so a host that answers
   for its users must start the run as that user.
3. **A stopped turn is followed, not failed.** `ConversationService.recover()` follows or settles a
   streaming turn's run (completed, paused, failed or cancelled with the run's outcome) and fails
   only a turn with no run, or whose run is gone, as `InterruptedResponse`. Keep calling it after
   `Runtime.recover()` (`Container.astart` does).
4. **Shutdown detaches turns.** `ConversationService.shutdown()` no longer cancels turns still
   running after its grace period: it leaves them `streaming` and their runs to
   `Runtime.shutdown()`, and the next start completes them. A user's cancel still cancels the turn
   and its run.
5. **A failed run's turn keeps its reason.** A turn whose run failed or timed out now has
   `metadata.error_type: "RunFailed"` (was `"RuntimeError"`), with `reason`, `run_status`,
   `retryable` and `usage`, plus `run_error_type` (the run's `RunResult.error_type`) and
   `limit_reached` when the run reports them, and a fixed text per reason from
   `ConversationPresentation.failure_messages`.
6. **A recovered run that had caller credentials waits for them.** A run started or resumed with
   `credentials=` and interrupted by a restart no longer continues without them (its tools saw no
   credentials and failed as "not signed in"). `recover()` pauses it in the new paused status
   `awaiting_credentials` with a `credentials.required` event; `resume(run_id, tenant_id, {},
   credentials=...)` continues it. Set `AgentRuntime.recovery_credentials` (or
   `build_container(recovery_credentials=...)`, or `recover(credentials_provider=...)`) to supply
   credentials (a service token, say) and continue such runs at once. Only a flag,
   `RunState.caller_credentials`, is stored; never the credentials. Clients that switch on
   `RunStatus` may see `awaiting_credentials`; `PAUSED_STATUSES`, `RUN_PAUSE_EVENT_TYPES` and
   `STREAM_END_EVENT_TYPES` include it, so `stream()` and the SSE run stream end on
   `credentials.required` by default. Runs started over HTTP never carry credentials and are not
   affected. A conversation turn whose run is paused this way becomes `awaiting_credentials` (new
   `MessageStatus`, `pending.kind: credentials`, event `assistant.message.awaiting_credentials`):
   continue it with `respond(..., ConversationCredentialsRenewal(), credentials=...)` or
   `POST /v1/conversations/{id}/turns/{turn_id}/credentials`. UIs that switch on message status
   should handle it.
7. **A child run's pause pauses its `core.subagent` parent.** When a delegated child run pauses for
   an approval or a clarification, its parent pauses too, in `awaiting_approval` or
   `awaiting_clarification`, with the child's pause payload plus `child_run_id`, `origin_run_id`,
   `child_agent` and `subagent_call_id`; before, the parent's model got the paused child's result
   and carried on. Approval inboxes that list `GET /v1/approvals` see the request once, under the
   child run. Answering the parent's pause answers the child's.
8. **An oversized tool result no longer fails the run by default.** When the newest tool results
   alone do not fit the context (`required context exceeds max_estimated_tokens`), each is
   offloaded to a context artifact the model pages with `core.context_read`, when the agent can
   call it, or else cut to what fits and replaced by a JSON object with `truncated: true`,
   `total_chars`, `kept_chars`, a `notice` and the cut `content` (the tool's `coverage`, if any,
   is kept, marked `capped`). The run's history keeps the shortened result. A
   `context.tool_result_truncated` event and audit record it. The run still fails when even empty
   results do not fit. Set `context_policy.oversized_tool_result: fail` to keep the 0.2.0b2
   behaviour. (G32)
9. **A failed workflow agent node keeps the child run's error type.** It raises the runtime error
   that ended the child run (`VerificationError` when its answer failed verification,
   `BudgetExceededError` at a limit, `PolicyDeniedError`, ...) or `TimeoutError` for a timed-out
   child, instead of a plain `RuntimeError`. The node's `error` now reads, for example,
   `VerificationError: final output failed verification`. Code matching `RuntimeError:` in node
   errors should match the type it expects. (RT-B19)
10. **Streams stopped part-way are charged.** A streamed model call that stops after a delta or
    tool call reached the caller (Stop, a closed client, a provider error mid-stream) is now
    settled with an estimate (the request's input estimate and about four characters per output
    token streamed) instead of released, since the provider bills it. Only a call that delivered
    nothing is released. A `UsageMeter` whose `settle` takes `estimated: bool = False` receives
    `estimated=True` for such a call; other meters get a plain `settle`.
11. **Deleting a conversation deletes its session memory.** `DELETE /v1/conversations/{id}` and
    `ConversationService.delete` also delete the memory session the turns wrote (the
    conversation's id), expired rows included, and its owner. Before, the memory (tool results
    included) stayed until `memory_retention_seconds`.
12. **Slow event subscribers.** A subscriber whose live queue fills up no longer loses the oldest
    events without notice: it reads on from the bus's history (PostgreSQL: nothing is lost). When
    the in-memory history no longer holds the missed events, the subscription raises
    `EventStreamOverflowError` with `last_sequence`, and the SSE run stream ends with a
    `stream.overflow` frame whose `id` is the last event delivered, so a client reconnecting with
    `Last-Event-ID` reads on from there.
13. **New event types.** Run events: `credentials.required` and `context.tool_result_truncated`.
    Conversation events: `agent.run.started` (`{run_id, agent}`), `assistant.message.steered`,
    `agent.steer.delivered`, `agent.steer.undelivered`, `assistant.message.awaiting_credentials`
    and, with `conversation_streaming.tool_steps`, `agent.tool.started` / `agent.tool.completed`.
    Consumers that validate event types against a closed list should add them; a client that
    ignores unknown types needs no change.
14. **`core.ask_user` is a built-in name.** Every `AgentRuntime` registers it, so a host tool of
    that name now raises `ConflictError` at registration; rename it or use the built-in.
15. **Stored records.** A conversation with `allowed_agents`, an assistant message with `agent`, a
    pending action with `choices` or `child_run_id`, a run with `caller_credentials`,
    `delegated_runs`, `error_type` or `limit_reached`, and an agent with
    `context_policy.oversized_tool_result: fail` or `max_context_tokens` carry the new fields;
    unset, they are stored and fingerprinted exactly as before, so 0.2.0b2 still reads them.

### You can now remove

- **Your own context builder kept only to change the token budget**
  (`DefaultContextBuilder(memory, max_estimated_tokens=...)` registered under another name, as
  algen-hrms does for 80,000): set `context_policy.max_context_tokens` on the agent.
- **Host-side restart handling of streaming turns** (marking them failed, re-running them, or
  looking their run up by time): the Runtime stores each turn's run id and settles it by the run's
  state; `ConversationService.follow` re-attaches.
- **Steering a conversation turn by looking up its run id**: call `ConversationService.steer` or
  `POST /v1/conversations/{id}/turns/{turn_id}/steer`.
- **Host code that forwards request headers to tools as credentials**: list them in
  `api.credential_headers`.
- **Your own `events.subscribe` loop that resubscribes after each pause** to follow a turn to its
  end: use `client.follow(run_id)` or `?through_pauses=true`. (A2)
- **Code that polls a delegated child run for its approval or question**, or a custom delegation
  tool written to surface it: the parent run shows the child's pause and answering it there
  resumes both. (A4)
- **A custom "ask the user" tool or planner** that pauses the run for a clarification: enable
  `core.ask_user`. (A5)
- **A host-side guard that cancels or re-authenticates runs recovered without credentials**: a
  recovered run waits for them, or takes them from `recovery_credentials`.
- **Host code that cuts or pages large tool results** before returning them only so a run does
  not fail with `required context exceeds max_estimated_tokens`: the Runtime now offloads or
  truncates the newest oversized result itself. Keep tool-side paging where the tool can return a
  better page than a cut.
- **Host code that catches a failed or limit-ended run and makes its own final "answer from what
  you have" model call**: set `budget.on_limit: summarize` and read `RunResult.partial`.
- **Host code that parses `RunResult.error`** or a workflow node's `RuntimeError:` message to tell
  a verification failure or a limit from other failures: read `RunResult.error_type` and
  `limit_reached`, or catch the specific error from the workflow node.
- **Custom `after_retrieval` policies whose only job is to screen retrieved chunks** for prompt
  injection with the built-in patterns: set `guardrail_policy.retrieval_injection`.
- **Hand-copied Runtime DDL** in a host's own migrations (Algen Studio's copies of `001`-`020`):
  generate it with `algen-agent-runtime migrations sql --since NNN`, and replace hand-written
  column checks with `check_schema(...).raise_for_issues()` in the host's tests.
- **Re-implemented PII or credential masking** (copies of `PIIRedactionPolicy().redact_value`,
  `security.redaction.redact` or the guardrail regexes): call `redact_pii` and `redact_secrets`.
- **Host-side user erasure or export scripts** that deleted Runtime rows table by table (and had to
  know the foreign-key order): call `container.user_data.erase_user` / `export_user`, the `users`
  CLI or the admin routes.
- **Host-side session memory cleanup after deleting a conversation**: the Runtime deletes the
  conversation's memory session itself.
- **Host-side top-up charges for stopped streams** that billed the reservation when a stream was
  cancelled: the Runtime settles an estimate itself.
- **Host-side approver checks in front of the workflow approval route**: the route now requires
  the run's user or `approvals:decide`; the input route requires the run's user.
- **Copies of the Runtime's Agent Skills kept by hand in an application repository**:
  `algen-agent-runtime skills install --path .` writes the ones matching the installed Runtime
  (`--force` refreshes them after an upgrade).
- **Host code for what the Runtime already provides**, now documented for hosts: conversation and
  turn state (`ConversationService`), restart follow-up of waiting turns
  (`reconcile_pending_turns`), lists of ending or pausing events (`STREAM_END_EVENT_TYPES`), PII
  and credential masking, and direct provider calls (`ModelRouter.generate` with `meter_call`). The
  `algen-runtime-host` skill's table says which release added each.

### Security

- `POST /v1/workflows/runs/{id}/approval` accepted a decision from any principal with
  `runs:write` in the tenant, including a service token, so a host could not separate who starts
  a workflow from who approves it. It now applies the run approval rule from 0.2.0b2: the
  workflow run's user, or a principal with `approvals:decide` (403 otherwise). The decider is
  recorded from the authenticated principal; the request body cannot name one. The in-process
  `WorkflowService.decide` is unchanged and does not check identity, like `Runtime.resume`.
- `POST /v1/workflows/runs/{id}/input` accepted an answer from any principal with `runs:write` in
  the tenant, so another user or a service token could put words in a run as its user. It now
  accepts only the workflow run's user (403 otherwise), as an agent run's `/clarification` does.
  The in-process `WorkflowService.provide_input` does not check identity.

### Fixed

- A conversation turn whose process was killed mid-run is no longer marked "interrupted, please
  retry" when its run was recovered and answered: the run id is stored on the assistant message as
  soon as the run starts (`agent.run.started`), and `ConversationService.recover()` follows the run
  to its end, or settles the turn at once if the run already completed (the answer), paused
  (`awaiting_approval`, `awaiting_input` or `awaiting_credentials` with its pending action, or
  `expired` if the approval expired meanwhile), failed or was cancelled. A turn whose run id was
  not stored yet finds its run by conversation and turn (`find_by_turn` on the run stores).
  Settling is a conditional message update (`update_message_if` on the conversation stores), so
  repeated or concurrent recovery settles a turn once, and recovery skips conversations this
  process is answering.
- A deploy no longer cancels every in-flight turn: shutdown detaches turns from their runs, the
  Runtime saves the runs for recovery, and the next start completes the turns.
- A failed turn keeps why its run failed (reason, status, token usage) and shows a user-safe text
  for known reasons, such as a usage meter's refusal, instead of a generic error.
- Two events published at once on one conversation no longer collide: sequences are allocated one
  at a time per conversation in a process, a sequence another process took is allocated again
  (PostgreSQL's unique violation is now a `ConflictError`), and a lost progress or delta event no
  longer fails a turn whose run goes on.
- Turns of different conversations no longer wait on one service-wide lock: each conversation has
  its own, kept only while in use.
- A run recovered after a restart continued without its caller's credentials; its conversation
  turn now waits for them as `awaiting_credentials` instead of being shown as an approval.
- A slow run event subscriber silently lost events, possibly `run.completed`.
- `RuleBasedPlanner` treats the clarification answer to a run started with empty input as its
  input, instead of asking again. (RT-B11)

### Added

- **`context_policy.max_context_tokens`** (1,024 to 2,000,000; unset by default): an agent's own
  estimated token budget for the default and retrieval context builders, instead of the builder's
  (16,000, or `retrieval.max_context_tokens`). `shorten_older_tool_results`,
  `oversized_tool_result` and `compaction_trigger_ratio` work against it; a model profile's
  `context_window_tokens` still caps what is sent. Left out of the agent's JSON while unset, so
  fingerprints do not change. `DefaultContextBuilder.token_budget(agent)` and
  `RetrievalContextBuilder.token_budget(agent)` return the budget an agent gets.
- `ConversationService.follow(conversation_id, turn_id, tenant_id, user_id)` re-attaches to a
  streaming turn after a restart (owner only, idempotent).
- `ConversationService.steer(conversation_id, turn_id, tenant_id, user_id, text)` and
  `POST /v1/conversations/{id}/turns/{turn_id}/steer` (`conversations:write`) send more
  instructions to a working turn's run.
- `ConversationService.create(..., allowed_agents=...)` and `submit(..., agent=...)` (and the
  message body's `agent`) let a turn choose its agent from the conversation's allowlist; the
  assistant message records it as `agent`.
- `submit(..., run_input=...)` gives the turn's run its own input text (a date prefix, attachment
  references), keeping the message text.
- `messages(..., user_id=...)` applies the owner check.
- `conversation_streaming.tool_steps` (`RuntimeConversationHandler(tool_steps=True)`) forwards tool
  calls as `agent.tool.started` and `agent.tool.completed` events, with names and sizes only.
- `api.credential_headers` forwards the listed request headers as caller credentials from the run
  and conversation routes (none by default).
- `ReattachableConversationHandler` (`follow_turn`, `turn_run`), `ConversationTurnInterruptedError`,
  `ConversationPresentation.failure_messages` and `run_failure_message`, `RUN_FAILURE_MESSAGES`,
  and `find_by_turn` on the in-memory, Redis and PostgreSQL run stores.
- `MessageStatus.AWAITING_CREDENTIALS`, `ConversationPendingAction.kind: "credentials"`, the
  `awaiting_credentials` turn outcome, `ConversationCredentialsRenewal` and
  `POST /v1/conversations/{id}/turns/{turn_id}/credentials` (`conversations:write`; the
  credentials are the `api.credential_headers` headers, 409 without any).
- `AlgenAgentRuntimeClient.follow(run_id, *, after=0)` and `through_pauses=True` on `stream()` and
  `stream_events()`: one subscription for a whole turn, through approvals, clarifications and
  credential pauses, ending only on `run.completed`, `run.failed` (including an expired pause) or
  `run.cancelled`. `GET /v1/runs/{run_id}/events?through_pauses=true` does the same over SSE and
  sends a `: keepalive` comment every 15 seconds while nothing happens. Defaults are unchanged.
  (A2)
- A `core.subagent` child's approval or clarification reaches the host on the parent: the
  parent's `approval.required` or `clarification.required` carries it with `child_run_id`.
  Deciding it on the parent, through the normal `resume` or `POST /v1/runs/{id}/approval` and
  `/clarification` (same actor checks and `approvals:decide`; `decided_by` is recorded on the
  child's approval), resumes the child, then the parent with the child's final result. Deciding
  it on the child continues the waiting parents too. Nested delegations surface at the root.
  Cancelling the parent cancels the waiting child. `RunState.delegated_runs` records the waiting
  calls, so the pause survives a restart. Conversation turns show it with
  `ConversationPendingAction.child_run_id` and the child's `tool`. (A4, G61)
- `core.ask_user`: a built-in tool the model calls to ask the user a question (`question`, optional
  `choices`). The run pauses in `awaiting_clarification` with a `clarification.required` payload of
  `kind: ask_user`; the answer, given with the usual clarification resume (`{"clarification":
  ...}`, `POST /v1/runs/{id}/clarification`, or a conversation turn's input answer), reaches the
  model as the call's result `{"answer": ...}`. Registered by every `AgentRuntime`; an agent
  enables it with `core.ask_user` in `enabled_tools` and `user.ask` in `tool_permissions`. Side
  effect `none`, never cached, retried or held back as a repeat. Works with every planner,
  including `react`. `ConversationPendingAction.choices` carries the choices. (A5, G56)
- `RunStatus.AWAITING_CREDENTIALS`, the `credentials.required` run event,
  `RecoveryCredentialsProvider`, `AgentRuntime.recovery_credentials`,
  `recover(credentials_provider=...)`, `build_container(recovery_credentials=...)` and
  `RunState.caller_credentials`.
- `EventStreamOverflowError`, and `InMemoryEventBus(subscriber_queue_size=...)`.
- `budget.on_limit: summarize`: a run that reaches its step, model-call, token, cost or time limit
  (and that `continuation_policy` does not extend) makes one last model call with no tools, capped
  by `budget.limit_answer_max_output_tokens` (1024) and `budget.limit_answer_timeout_seconds` (30),
  to answer from what it gathered. The answer is verified with no repair turn; the run completes
  with `RunResult.partial: true` and `limit_reached`, and `run.completed` carries
  `outcome: partial` and `limit`. The default, `fail`, is unchanged. (G30)
- `RunResult.error_type` names the runtime error that ended a failed run (`VerificationError`,
  `BudgetExceededError`, ...), and `RunResult.limit_reached` the limit it reached. Both, and
  `partial`, are left out of JSON while unset. `run_failure_error(result)` in
  `algen_agent_runtime.exceptions` turns a run that did not complete back into that error.
  (RT-B19)
- `guardrail_policy.retrieval_injection` (`off` by default, like `tool_result_injection`; `flag`
  or `drop`) screens retrieved evidence chunks with the same check as tool results. `flag` keeps a
  matching chunk, marked as untrusted data not to follow; `drop` leaves it and its citation out.
  Each build that matches emits `policy.evaluated` (`boundary: after_retrieval`,
  `reason_code: retrieval_injection.detected`) and an audit with the chunk ids. (G10)
- `context_policy.oversized_tool_result` (`truncate`, the default, or `fail`). (G32)
- **`algen-agent-runtime migrations list` and `migrations sql [--since NNN] [--schema NAME]
  [--no-record]`** print the Runtime's own migrations, in order, from the installed package (no
  database needed), each with the history row the Runtime writes; `persistence.migrations()` and
  `migration_sql()` give the same in Python. (A1)
- **`check_schema(target, *, schema=None)`** (`algen_agent_runtime.persistence`) compares a live
  schema with what the migrations create and returns a `SchemaReport` of missing tables, columns,
  defaults, indexes and triggers, type and NOT NULL differences, and migrations missing from the
  history table; `report.raise_for_issues()` fails a host's test. It only reads the catalog. The
  expected catalog ships as `persistence/schema_catalog.json` (regenerate with
  `scripts/schema_catalog.py --update`). (A1)
- **`algen_agent_runtime.security.redact_pii(value, *, entities=None, exempt=(), schema=None)`
  and `redact_secrets(value)`**, the `pii` and `secrets` guardrails' own rules as documented
  functions over strings, dicts and lists (`PII_ENTITIES` lists the kinds). (A3)
- **`UserDataService`** (`container.user_data`, `algen_agent_runtime.persistence`):
  `erase_user(tenant_id, user_id, *, dry_run=False)` and `export_user(tenant_id, user_id, *,
  include_artifact_data=False)` across runs, run events, approvals, tool executions, artifacts,
  session memory and owners, learned memory, conversations (messages, events, feedback), workflow
  runs and the user's MCP connections, in memory, Redis and PostgreSQL. Erasure is idempotent,
  deletes children first, refuses while the user has work in progress (including a turn awaiting
  credentials), and is audited (`user_data.erased`, `user_data.exported`); audit events are kept.
  A store that cannot do it raises the new `UserDataUnsupportedError`. CLI
  `algen-agent-runtime users erase|export`, and `GET`/`DELETE /v1/admin/users/{user_id}/data` with
  the new `users:admin` scope. (G39)
- Store methods behind it: run stores `list_for_user`, `delete_many`; memory stores
  `sessions_for_user`, `session_owner`, `delete_session` (the new `SessionDeletion` protocol);
  event stores `forget_runs`; approval and tool execution stores `list_for_runs`, `forget_runs`;
  learned memory `list_for_user`, `forget_user` (`UserMemoryRecord`); workflow checkpoint stores
  `list_for_user`, `delete_many`; MCP token stores `servers_for_user`, `forget_user`.
- `metering.accepts_estimated(meter)` and the `model_stream_settled_with_estimate` log line.
- `algen-agent-runtime skills list` and `skills install --path DIR [--skill NAME]
  [--skills-dir DIR] [--force]`. The wheel ships the user-facing Agent Skills
  (`algen-agent-builder`, `algen-agent-connector`, `algen-runtime-extension`,
  `algen-runtime-host`) under `algen_agent_runtime/_skills`; `install` copies them into a
  project's `.agents/skills/`, prints each file, and refuses to overwrite an installed skill
  without `--force`. The maintainer skills stay in the repository.
- The `algen-runtime-host` Agent Skill, for applications that embed the Runtime: a table of what
  a host needs, the Runtime API that provides it, the release that added it, and when to keep
  your own.

### Documentation

- `docs/conversations.md`: `ConversationService` from a host (the calls and which check the
  owner, following a turn, paused turns including credentials, delegated pauses and `ask_user`,
  steering, agents per turn, failures, restarts and recovery by run, credentials, attachments,
  sessions and deletion, custom handlers).
- "Reusing the Runtime's redaction" (`docs/agent-tooling.md`), "Following a run from a host" and
  "Following a run through its pauses" (`docs/operations.md`) and "One-off model calls"
  (`docs/providers.md`), with matching sections on the docs page.
- `docs/operations.md`: "Hosts that own the schema: `migrations` and `check_schema`", "Erasing or
  exporting one user's data", migrations 021 and 022 in the upgrade table, conversation deletion
  and memory, `users:admin` and the workflow approval rule in the scope table, stopped streams in
  usage metering, conversation turns awaiting credentials. `docs/agent-tooling.md`: "Reusing the
  redaction rules" and the estimate for stopped streams.
- `docs/context-management.md`: "The context budget of an agent".
- `docs/threat-model.md` and `docs/multi-agent-workflows.md`: who may decide a workflow approval.
- `docs/docs.html`: the 0.2.0b3 content, gated `data-version-min="0.2.0b3"`.
- The changelog's "You can now remove" subsection, and the contributor checklist step that fills
  it. The `algen-runtime-host` skill's table covers every 0.2.0b3 capability.

## [0.2.0b2] - 2026-11-11

Fixes from an independent review of 0.2.0b1: four tenant-isolation and privilege issues, run
output that failed or changed after verification, streams that never ended on a pause, a recovery
race between two processes, and PII, secrets and injection rules that refused ordinary data. Run
approvals now record who decided them, the client passes caller credentials, and `core.subagent`
can be enabled from settings. Migrations 019 and 020 are additive. The public surface only grows (`docs/api/surface.json`). Several fixes
change behaviour: read the upgrade notes.

### Upgrade notes

1. **Sessions belong to their user.** A run that names another user's `session_id` in the same
   tenant is refused (`NotFoundError` in process, 404 over HTTP). Give each user their own session
   id. Generated session ids are claimed at the first memory write and stay with their user for the
   memory's retention. A memory store without `claim_session` is not checked. (RT-B02)
2. **Who may act on a run over HTTP.** For a run that was not started from a conversation, only the
   run's user may decide its approval, cancel, steer or answer it. Approvers working from
   `/v1/approvals` need the `approvals:decide` scope; operators who cancel other users' runs need
   `runs:admin`. Reads stay tenant-wide with `runs:read`. (RT-G03)
3. **`core.subagent` needs an allowlist.** An agent delegates only to the agents in its new
   `delegates` list (empty allows none, `"*"` allows any), and not to an agent whose
   `tool_permissions` exceed its own. The nesting depth is counted on the server from
   `parent_run_id`; the model's `depth` argument is ignored, and the limit raises
   `PolicyDeniedError`. Delegations are never cached or retried. (RT-B03, RT-B04, RT-B18, RT-G14)
4. **`serve --host` is validated.** With development-header auth, `serve --host` on a non-loopback
   address now refuses to start (unless `allow_insecure_development_auth`), and `create_app`
   re-validates API settings built with `model_copy`. (RT-B01)
5. **A failed run has no output.** `RunResult.output` is `None` unless the run completed, and the
   run view's `output_text` is null for failed, timed-out and cancelled runs. A verification
   failure keeps the rejected answer in `RunResult.rejected_output` (left out of JSON) and the run
   view's `rejected_output`. (RT-B05)
6. **Structured output and guardrails.** With a `response_schema`, `after_model` and
   `final_response` policies receive the parsed `dict` or `list` (context
   `structured_output: True`) instead of a string; custom policies there must handle it. The
   built-in redactions change string values only. A `final_response` change to verified structured
   output is verified again, and the run fails if it no longer passes. (RT-B06)
7. **Streams end on a pause.** `client.stream()` and `GET /v1/runs/{id}/events` end after
   `approval.required`, `clarification.required` or `tool.reconciliation_required`. Follow a resumed
   run with `client.stream_events(run_id, after=...)` or reconnect with `Last-Event-ID`. (RT-B08)
8. **Fewer false redactions and refusals.** A 12-digit number is an Aadhaar number only when it
   passes the Verhoeff check; a 10-digit number is a phone number only with `+91`, `91` or `0`, 5-5
   grouping, or a phone word nearby or in its field name. Prose such as "the secret: consistency"
   is no longer masked, and injection screening needs an instruction addressed to the assistant
   ("ignore previous instructions" is still refused anywhere). Deployments that relied on bare
   10-digit numbers being masked should name the field, add phone context or use their own DLP
   policy. (RT-B12 to RT-B15)
9. **Working-state updates no longer pause for approval.** `core.working_state_update` declares no
   side effect; `context.write`, the memory-write policies and the revision check still apply.
   (RT-G45)
10. **Recovery is single-node.** Enable `recover_incomplete_runs` on exactly one process per
    database until the 0.3 leases. A second process no longer fails to start or fails the run when
    both recover the same database. (RT-B07)
11. **Approvals record who decided.** Migration `020_approval_decided_by.sql` adds nullable
    `decided_by`, `decided_at` and `comment` columns to the approvals table; requests decided
    earlier have none. A custom `ApprovalService` whose `decide` lacks the new `decided_by` and
    `comment` keywords keeps working but records no approver, and the Runtime logs
    `approval_service_records_no_approver` once; add
    `*, decided_by: str | None = None, comment: str | None = None` to record it. A custom `Runtime`
    used with `RuntimeConversationHandler` receives `decided_by` only if its `resume` accepts it.
    (RT-G01)
12. **`core.subagent` from settings.** A host that registers `subagent_tool(runtime)` itself on a
    `build_container` container must not also set `runtime.subagent.enabled: true`; doing both
    raises `ConflictError`. (G62)

### Security

- Session memory was shared by users of a tenant who named the same `session_id`; sessions are now
  bound to their first user, in memory, Redis and PostgreSQL stores (migration
  `019_session_owners.sql`, swept with the memory). (RT-B02)
- `serve --host` could publish development-header auth on a public interface. (RT-B01)
- `core.subagent` could return another user's cached delegation, let the model choose its nesting
  depth, and run any agent of the tenant. (RT-B03, RT-B04, RT-G14)
- Any tenant user with `runs:write` could approve, cancel or steer another user's run that was not
  started from a conversation. (RT-G03)

### Fixed

- A run that failed verification returned the rejected answer as its output. (RT-B05)
- Redaction rewrote verified JSON into text that no longer parsed, or changed it after
  verification while the run reported `completed`. (RT-B06)
- `AlgenAgentRuntimeClient.stream()` and the SSE run stream never ended when the run paused.
  (RT-B08)
- `build_container(additional_tools=...)` accepts `Toolkit` instances and registers them as a plugin
  does: the toolkit becomes a container resource, opens with the container's `env://` secrets and
  closes with it. Before, a Toolkit's tools passed there never got `register(context)`. (RT-B20)
- Two processes recovering one PostgreSQL database could fail a start with `RunConflictError` or
  mark the interrupted run failed on an event-sequence collision. (RT-B07)
- Tools marked `volatile` were stored in and served from the tool-result cache. (RT-B09)
- A delegated child run did not set `RunRequest.parent_run_id`. (RT-B18)
- The `pii`, `secrets` and `prompt_injection` rules refused or redacted tracking and order numbers,
  Telegram chat ids, prose mentioning a secret, and trust-and-safety questions. (RT-B12 to RT-B15)

### Added

- `AgentDefinition.delegates` and `agents[].delegates`. (RT-G14)
- Tool schema fields declaring `"format": "identifier"` or `"x-pii": false` keep a one-token value
  at `plan_tool`, `before_tool`, `after_tool` and in history; `guardrail_policy.pii.exempt_entities`
  turns kinds off per agent. Both markers are removed from the tool schemas sent to model providers,
  some of which refuse unknown keywords and formats. (RT-G08)
- `RunResult.rejected_output`, `AlgenAgentRuntimeClient.stream_events()` and
  `STREAM_END_EVENT_TYPES`.
- The `SessionOwnership` protocol (`claim_session`) on the memory stores, the `approvals:decide`
  and `runs:admin` scopes, and a `session_owners` retention sweep.
- A run's approval request records who decided it: `ApprovalRequest.decided_by`, `decided_at` and
  `comment`, returned by `GET /v1/approvals`. `POST /v1/runs/{id}/approval` fills `decided_by` from
  the authenticated principal (the body cannot name it) and takes an optional `comment` (at most
  4,000 characters); conversation turns record the conversation's owner. New keywords:
  `ApprovalService.decide(..., decided_by=, comment=)` and `Runtime.resume(..., decided_by=)`.
  (RT-G01)
- `AlgenAgentRuntimeClient.run`, `run_sync` and `stream` take `credentials=`, passed to the run like
  `Runtime.run(..., credentials=)`. (RT-B17)
- `runtime.subagent.enabled` (default `false`) registers `core.subagent` in `build_container`, and
  `runtime.subagent.max_depth` (default 3, 1 to 10) bounds a delegation chain. An agent still opts
  in with `core.subagent` in `enabled_tools`, `agents.delegate` in `tool_permissions` and the agents
  it may call in `delegates`; `describe` and `check` list the tool when it is enabled. (G62)

### Documentation

- 0.1-only claims are shown only for 0.1 on the docs page: substring-only recall, deprecated
  document extractors, the 0.1.0a14 meter `reserve` form, an approval that "covers the step, not its
  arguments", and pinned 0.1 install banners. Strict approval binding and similarity recall are
  described for 0.2. (D2 to D7)
- Approval attribution is claimed for workflow approvals, and for run approvals from 0.2.0b2;
  citations are described as markers
  checked against sources; `assert_tool_contract` lets free-form maps pass; Toolkits can be passed
  to `additional_tools`; a failed child agent run surfaces as `RuntimeError` in workflows;
  recovery is documented as single-node. (D8, D9, D11 to D14)

## [0.2.0b1] - 2026-11-11

The first `0.2` beta. Everything deprecated during `0.1` is removed, the server and OTLP
dependencies become optional, the examples move to their own package name, and approvals are bound
to the exact call they approve. The public API is a candidate for freezing: later `0.2` betas take
only fixes and additive changes. Read the upgrade notes first; migrations 017 and 018 are additive.

### Upgrade notes

1. **Install the server explicitly.** `fastapi`, `uvicorn` and the OTLP exporter are now extras:
   `pip install 'algen-agent-runtime[service]'` for a deployable service (`server` and `otlp` on
   their own). Without them `algen-agent-runtime serve` exits with the install command, and
   importing `algen_agent_runtime.api.app` or setting an OTLP endpoint raises `MissingExtraError`
   (a `ConfigurationError` and a `ModuleNotFoundError`). The container image installs `[service]`.
   (#115)
2. **Rename `examples`.** The installed package is `algen_agent_runtime_examples`:
   `python -m examples.quickstart_approval.app` becomes
   `python -m algen_agent_runtime_examples.quickstart_approval.app`, and hook providers and plugins
   named `examples.<name>...` in configuration become `algen_agent_runtime_examples.<name>...`.
   There is no shim. (#115)
3. **Removed:** `algen_agent_runtime.communications` (use `algen_agent_tools.mail` and the SMTP or
   Gmail toolkit), `tools.adapters.database_tool` (use `PostgresToolkit.query`), and the document
   extractors in `algen_agent_runtime.security` (`documents`, `docx`, `pptx`, `xlsx`, `html`,
   `ooxml`): import the same names from `algen_agent_tools.documents.inspection` and install
   `algen-agent-tools[documents]`. The `documents` extra is gone. (#81, #88)
4. **Usage meters must take `call`.** A `UsageMeter` whose `reserve()` has no `call` parameter (the
   `0.1.0a14` form) is refused with `ConfigurationError` when the router is built.
5. **`ToolExecutor.execute(approval=)` takes a `ToolApproval`**, not a string: build it with
   `ToolApproval.for_call(approval_id, "approved", tool, version, arguments)` after your own
   decision. A call the approval does not cover raises `ApprovalMismatchError`. (#117)
6. **Workflow approvals bind the exact call** (#109). An approval-required tool step runs only with
   the arguments its approval showed (or the approver's modified parameters); a step whose
   arguments differ, read values produced after the approval, or ignore a modification is refused
   with `workflow.tool.refused`. Template the step's arguments from what the approval shows
   (`<output_key>.parameters`, `review_from` values, literals) or move the approval after the nodes
   that produce them. `binding: step` on the approval node keeps the old behaviour and is
   discouraged; `check` reports `APPROVAL_ARGUMENTS_UNBOUND` as an error. Approvals decided before
   the upgrade run as before.
7. **A resumed run may ask again.** If an approved tool's definition or code changed between the
   request and the decision, the approval expires with `status_reason: tool_changed` and the run or
   workflow pauses for a new one (`replaces_approval_id`). Clients must be ready to show it. (#127)
8. **Custom `ApprovalService` implementations** add `expire()` and accept the new `create()`
   keywords (`tool`, `tool_version`, `tool_fingerprint`, `arguments_sha256`, `rationale`).
9. **Policy actions per boundary changed** (#116):
   - the built-in secrets policy answers `redact` (`policy.redacted`), not `transform`;
   - `plan_tool` refuses `redact` and `require_clarification` (they were ignored), receives the
     agent, and uses `guardrail_policy.tool_policies` like `before_tool`: list a custom plan-time
     policy there or register it `mandatory=True`;
   - `transform` at `plan_tool` or `before_tool` now rewrites the arguments (re-validated against
     the input schema) instead of refusing the call. Answer `deny` to keep refusing.
10. **Attachments** (#130, #131): `attachments.types` may list only types a model reads; move
    `text/csv`, `image/heic` and similar to the new `tool_read_types`. `security.allowed_attachment_types`
    now defaults to every known type. The text placeholder is `[attachment: name, type, id=<id>]`.
    The upload scanner marks a document whose bytes match its type `clean`, and quarantines one
    that claims to be an image or PDF as `type_mismatch`.
11. **Context packing** (#133): with `shorten_older_tool_results`, older tool exchanges are kept
    shortened and whole results get about three quarters of the budget
    (`shortened_tool_results_share`). A stray tool result is dropped with a warning instead of
    raising `ConflictError`; call `message_groups(messages, strict=True)` to keep the error.
12. **Rollback:** migrations 017 and 018 can stay in place under `0.1.0a23`. A workflow checkpoint
    with `approved_calls`, or a run state with `tool_approvals`, cannot be read by `0.1.0a23`;
    finish or cancel those before rolling back.

### Added

- `GovernedToolBridge` hands Runtime tools to the OpenAI Agents SDK, LangGraph/LangChain (and, via
  `bridge.call`, AutoGen or CrewAI); each call goes through the Runtime's tool executor. Tests with
  the real SDKs pin which calls inside a framework the Runtime governs. (#119)
- Approval `rationale`: the model's own text that came with the call, redacted and capped at 500
  characters, on the approval record and API, the pause, `approval.required` and pending
  conversation actions. (#120)
- Tool fingerprints (`tool_fingerprint`, `Tool(source=...)`) recorded when approval is requested;
  migration 017 adds `tool`, `tool_version`, `tool_fingerprint`, `arguments_sha256`, `rationale`
  and `status_reason` to approvals. (#127)
- Similarity-ranked learned-memory recall: `learned_memory_policy.recall: semantic | hybrid`, with
  `min_score`, `hybrid_keyword_weight` and an embedding provider. Migration 018 stores vectors
  without needing the pgvector extension, and uses pgvector's operator when it is installed. (#124)
- `attachments.tool_read_types`: Office documents (OOXML and legacy), Outlook and RFC 822 mail,
  and text files attached to a run for a tool to read; they never reach a provider. (#130)
- A tool can hand images and PDFs to the model: `ToolResult.files` with
  `ToolDefinition.returns_files`, checked like run attachments. (#129)
- `ResultCoverage`, a tool result's account of what it searched, and the opt-in
  `result_coverage` verifier, which stops an answer stating absence on a partial search. (#135)
- A bounded thread pool for synchronous tools (`runtime.tool_threads`; `ToolCapacityError` when
  full) and `ToolDefinition.execution: process`, which kills a call at its timeout. (#114)
- `examples/README.md` index and the offline `quickstart_attachments` example. (#97, #98)
- A table of the actions each policy boundary supports, pinned by contract tests. (#116)
- The public surface is recorded in `docs/api/surface.json` (exports with signatures, HTTP routes,
  configuration fields, errors, event identifiers, `check` codes, protocols, migrations), and
  `tests/unit/test_api_surface.py` fails on any change to it that the file does not record. Later
  `0.2` betas only add to it.

### Fixed

- **The `approval` scaffold no longer approves by itself.** Its `app.py` asks a person by default,
  fails closed without an answer (exit status 2, nothing applied), and decides headless only with
  `--approve-all` or `--reject-all`; `--decision` is gone. Generated tests pass the decision
  explicitly.
- Generated workflow projects run through the workflow service: the hook module is
  `<project>_hooks.py`, listed in `security.trusted_plugin_prefixes`, and the README says how to
  serve it. Before, the service listed their workflows as unavailable and refused to start them.
- Generated workflows read the run's text from `input`, where the workflow service stores what a
  caller posts; they read `question`, which only the local `app.py` set, so a run started over
  HTTP proposed "Default action" and its model saw no text. They also declare their output
  (`output_key: result` for the approval workflow, `final_summary` with an `output_schema` for the
  DAG), so `GET /v1/workflows/runs/{id}` returns it instead of `output: null`.
- An approval decision the workflow refuses (a modification that fails `parameters_schema`, a
  modification the node does not allow) is refused by `POST /v1/workflows/runs/{id}/approval`
  with 400 or 409 and the reason. It answered 202 with the run unchanged, and only the server log
  said why. `MultiAgentWorkflowExecutor.check_approval_decision` runs the same checks without deciding.
- `check` no longer reports `POST_BUILD_REGISTRATION` for `register_handler` on a
  `WorkflowHookRegistry` a hook provider returns, and reports a hook provider outside
  `security.trusted_plugin_prefixes` as `HOOK_PROVIDER_UNTRUSTED` (an error for
  `--profile standalone`, a warning for Studio, which reviews hook modules itself).
- A streamed model call falls back to the next candidate when the provider fails before its first
  event; failures count against the provider's health. (#106, contributed by Abhishek Patel)
- An approved call runs with the arguments its approver approved: a `plan_tool` transform is not
  applied to them again on resume.
- A model profile with `context_window_tokens` counts attached files at their estimated cost
  instead of raising `ConfigurationError`.

### Changed

- The 0.1 history moved to [CHANGELOG-0.1.md](CHANGELOG-0.1.md).
- The upgrade and rollback drills run against `0.1.0a23` (they ran against `0.1.0a17`), and check
  that the migrations they apply as the previous release match its tag.
- `scripts/ci-local.sh` builds and tests the way CI does (sdist, wheel from the sdist, artifact
  checks, a clean install, the base job on Linux); CI and the release workflow run the same
  `scripts/check_dist.py` and `scripts/smoke-install.sh`.
- Development dependencies allow mypy 2, pytest-cov 7 and redis-py up to 8 (#94). `mcp` 2 and
  `transformers` 5 are not supported yet.

## [0.1.x]

The `0.1` pre-releases (`0.1.0a1` to `0.1.0a23`) are in [CHANGELOG-0.1.md](CHANGELOG-0.1.md).

[Unreleased]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.2.0b3...HEAD
[0.2.0b3]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.2.0b2...v0.2.0b3
[0.2.0b2]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.2.0b1...v0.2.0b2
[0.2.0b1]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a23...v0.2.0b1

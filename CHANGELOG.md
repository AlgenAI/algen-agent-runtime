# Changelog

All notable changes to Algen Agent Runtime will be documented in this file.
The project follows [Semantic Versioning](https://semver.org/) while public
contracts are explicitly marked experimental during the `0.x` series.

## [Unreleased]

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

[Unreleased]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.2.0b2...HEAD
[0.2.0b2]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.2.0b1...v0.2.0b2
[0.2.0b1]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a23...v0.2.0b1

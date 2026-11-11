# Changelog

All notable changes to Algen Agent Runtime will be documented in this file.
The project follows [Semantic Versioning](https://semver.org/) while public
contracts are explicitly marked experimental during the `0.x` series.

## [Unreleased]

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

### Fixed

- A streamed model call falls back to the next candidate when the provider fails before its first
  event; failures count against the provider's health. (#106, contributed by Abhishek Patel)
- An approved call runs with the arguments its approver approved: a `plan_tool` transform is not
  applied to them again on resume.
- A model profile with `context_window_tokens` counts attached files at their estimated cost
  instead of raising `ConfigurationError`.

### Changed

- The 0.1 history moved to [CHANGELOG-0.1.md](CHANGELOG-0.1.md).
- Development dependencies allow mypy 2, pytest-cov 7 and redis-py up to 8 (#94). `mcp` 2 and
  `transformers` 5 are not supported yet.

## [0.1.x]

The `0.1` pre-releases (`0.1.0a1` to `0.1.0a23`) are in [CHANGELOG-0.1.md](CHANGELOG-0.1.md).

[Unreleased]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a23...HEAD

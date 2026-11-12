# What the Runtime already provides to a host

Doc paths are relative to <https://github.com/AlgenAI/algen-agent-runtime/blob/main/>; the
documentation page is <https://algenai.github.io/algen-agent-runtime/>. "Since" is the first
release with the API. `container` is what `build_container(settings)` returns, `runtime` is
`container.runtime` and `service` is `container.conversations`.

## If you need X, use Y

| You need | Use | Since | Keep your own only if |
| --- | --- | --- | --- |
| Conversations, messages and turns, with an owner | `ConversationService`: `create`, `list(tenant_id, user_id)`, `messages(..., user_id=...)` (owner check, 0.2.0b3), `submit`, `update`, `delete` (also deletes the conversation's memory session, 0.2.0b3), `cancel`; `storage.conversation_store: postgres`. docs/conversations.md | 0.1.0a1 | the conversation is not a chat with one user (a shared case file, say); still back each turn with a Runtime run |
| A turn that waits for an approval, an answer or the caller's credentials, and resumes into the same message | `ConversationService.respond(conversation_id, turn_id, tenant_id, user_id, ConversationApprovalDecision(...) \| ConversationInputAnswer(...) \| ConversationCredentialsRenewal(), credentials=...)`; the message's `pending` action (`kind`: `approval`, `input`, `credentials`; `choices` and `child_run_id` from 0.2.0b3); `POST /v1/conversations/{id}/turns/{turn_id}/approval`, `/input`, `/credentials`. docs/conversations.md | 0.1.0a16; `credentials` kind 0.2.0b3 | never; a custom handler implements `ResumableConversationHandler` instead |
| An approvals inbox | `runtime.approvals.list(tenant_id, status=..., run_id=..., agent=..., limit=..., offset=...)`, `GET /v1/approvals` (`approvals:read`). docs/operations.md, "Approval inbox" | 0.1.0a18 | you join approvals with domain records; read them from the Runtime, do not copy them |
| Who decided an approval | `ApprovalRequest.decided_by`, `decided_at`, `comment`; `runtime.resume(..., decided_by=user_id)`; conversation turns record the owner | 0.2.0b2 | never; pass the real approver's id |
| Who may decide or cancel another user's run | scopes `approvals:decide` and `runs:admin` on the HTTP API; from 0.2.0b3 `approvals:decide` also governs workflow approvals (`POST /v1/workflows/runs/{id}/approval`: the run's starting user, or `approvals:decide`; `decided_by` is the authenticated user), and `POST /v1/workflows/runs/{id}/input` is the run's user's only. In Python `runtime.resume`, `WorkflowService.decide` and `provide_input` do not check who acts: check it in the host before calling | 0.2.0b2; workflows 0.2.0b3 | in-process hosts check their own roles before `resume`, `decide` and `cancel` |
| Follow one run's events | `runtime.events.subscribe(run_id, after=n)` (never ends by itself); `AlgenAgentRuntimeClient(runtime).stream(request)` and `.stream_events(run_id, after=n)` end on an ending or pausing event; `client.follow(run_id, after=0)` (or `through_pauses=True`) keeps one subscription through pauses and ends on `run.completed`/`failed`/`cancelled`; SSE `GET /v1/runs/{id}/events` with `Last-Event-ID` (`?through_pauses=true`, a `: keepalive` comment every 15 s). Bound a Python follow with `asyncio.timeout`. docs/operations.md, "Run event streams" | subscribe 0.1.0a1; `stream_events` 0.2.0b2; `follow` 0.2.0b3 | never |
| Know which events end or pause a run | `RUN_END_EVENT_TYPES`, `RUN_PAUSE_EVENT_TYPES` (includes `credentials.required` from 0.2.0b3), `STREAM_END_EVENT_TYPES` in `algen_agent_runtime.events.contracts` | 0.2.0b2 | never; a hand-written list misses `tool.reconciliation_required` and `credentials.required` |
| Follow a conversation | `service.events.subscribe(conversation_id, after=n)`; SSE `GET /v1/conversations/{id}/events` with `Last-Event-ID` | 0.1.0a1 | never |
| Restart safety | `runtime.recover_incomplete_runs` (default `true`): `container.astart()` runs `Runtime.recover()` then `ConversationService.recover()`. Each turn stores its run id at start (`agent.run.started`); recovery finds the run (or `find_by_turn`) and never starts it again: a working run is followed into the same message, a finished, paused, failed or cancelled run settles the turn, only a turn with no run fails as interrupted (retryable). `service.follow(conversation_id, turn_id, tenant_id, user_id)` re-attaches; `shutdown()` detaches turns instead of cancelling them. Single-node: enable it on one process per database | recover 0.1.0a1; `reconcile_pending_turns` 0.1.0a16; turn recovery by run 0.2.0b3 | never; do not re-run turns yourself, that repeats side effects |
| Let the user add to a running run | `runtime.steer(run_id, tenant_id, text)`, `POST /v1/runs/{id}/steer`; `RunResult.undelivered_steering`. For a conversation turn: `ConversationService.steer(conversation_id, turn_id, tenant_id, user_id, text)`, `POST /v1/conversations/{id}/turns/{turn_id}/steer`. docs/operations.md, "Steering a running run" | runs 0.1.0a18; conversation turns 0.2.0b3 | never route steering through tool results |
| A model call outside a run (a title, a summary, a classification) | `runtime.router.generate(ModelRequest(...), (agent.default_model, *agent.fallback_models), meter_call=MeterCall(tenant_id=..., user_id=...))`. docs/providers.md, "One-off model calls" | router 0.1.0a1; `meter_call` 0.1.0a15 | never call `provider.generate` directly |
| Count or limit model usage | `build_container(usage_meter=...)` with a `UsageMeter` (`reserve(*, max_input_tokens, max_output_tokens, call)`, `settle`, `release`); `MeterCall` says whose call it is. docs/agent-tooling.md, "Metering model calls" | `UsageMeter` 0.1.0a14; `MeterCall` 0.1.0a15 | your billing ledger stays yours; the meter is where it is charged |
| Mask PII in data the host stores or shows | `redact_pii(value)` from `algen_agent_runtime.security` (0.2.0b3), or `PIIRedactionPolicy().redact_value(value)` (`algen_agent_runtime.policies.engine`): emails, card numbers, Aadhaar and phone numbers, at any depth. docs/agent-tooling.md, "Reusing the Runtime's redaction" | 0.1.0a18; `redact_pii` 0.2.0b3 | you need jurisdiction-specific identifiers; register your own DLP policy too |
| Mask credentials in data the host logs or stores | `redact_secrets(value)` from `algen_agent_runtime.security` (0.2.0b3); `redact(value)` (sensitive keys, bearer tokens, `key=value` credentials), `redact_text(text)`, `redact_parameters(arguments)` in `algen_agent_runtime.security.redaction` | `redact` 0.1.0a1; the others 0.1.0a18; `redact_secrets` 0.2.0b3 | never |
| Stop a model looping, repeating or overusing a tool | `tool_policy`: `max_refusals_per_tool`, `repeat_guard`, `max_calls_per_tool`, `on_error: return_to_model`; `wrap_up_policy`; `context_policy.shorten_older_tool_results`. docs/agent-tooling.md, "Tool-call guards" | 0.1.0a14 (`on_error` 0.1.0a1) | never as prompt rules; prompts are not enforced |
| A structured answer | `RunRequest(overrides=RequestOverrides(response_schema={...}))`: the `json_schema` verifier checks it and repairs; `RunResult.rejected_output` keeps a rejected answer for diagnostics. docs/verification.md | `response_schema` 0.1.0a1; `rejected_output` 0.2.0b2 | never parse and re-prompt yourself; never show `rejected_output` as the answer |
| Sessions that belong to one user | the run's `user_id`: a run naming another user's `session_id` is refused (`NotFoundError`); conversations use their own id as the session | 0.2.0b2 | never; pass the signed-in user, not a service or demo user |
| Tools that act as the signed-in user | `credentials=` on `runtime.start`, `run`, `resume`, `AlgenAgentRuntimeClient.run`/`stream`, `service.submit`/`respond`; tools read `ToolContext.credentials`. Over HTTP, `api.credential_headers` lists the request headers forwarded as credentials (none by default). A run recovered after a restart pauses in `awaiting_credentials` (`credentials.required`) until `resume(run_id, tenant, {}, credentials=...)`, or give `build_container(..., recovery_credentials=provider)`. docs/tool-authoring.md, "Credentials of the caller" | runs 0.1.0a15; conversations 0.1.0a16; client 0.2.0b2; `credential_headers` and recovery pause 0.2.0b3 | never put a user's token in settings or in the request metadata |
| Files with a run or a message | upload as an artifact (`POST /v1/artifacts`), then `RunRequest.attachments` or `service.submit(..., attachments=(RunAttachment(...),))`. docs/attachments.md | 0.1.0a19 | never send files inline in the prompt |
| The Runtime's tables | `PostgresDatabase` runs every Runtime migration at start (`initialize_schema`); `storage.postgres_schema` keeps them apart. A host whose migration tool owns DDL takes the SQL from `algen-agent-runtime migrations sql [--since NNN] [--schema NAME]` (`migrations list`) and checks the result with `await algen_agent_runtime.persistence.check_schema(dsn_or_db, schema=...)` then `.raise_for_issues()`. docs/operations.md, "Hosts that own the schema" | migrations 0.1.0a1; schema 0.1.0a18; `migrations` CLI and `check_schema` 0.2.0b3 | your platform forbids DDL at start; then apply `migrations sql`, never copied DDL, and run `check_schema` in tests and deploys |
| A delegate's approval or question | it surfaces on the parent's `approval.required` / `clarification.required` (and the turn's `pending`) with `child_run_id`; decide or answer on the parent as usual: the child resumes, then the parent | 0.2.0b3 | never resume the child yourself |
| Let the agent ask the user | `core.ask_user` in `enabled_tools` and `user.ask` in `tool_permissions`; read `pause_payload.question` / `choices` (a turn's `pending.question` / `choices`); answer `{"clarification": "..."}` (`ConversationInputAnswer`) | 0.2.0b3 | never as a tool that returns a sentinel |
| A subscriber that fell behind | `EventStreamOverflowError.last_sequence`; the SSE stream ends with a `stream.overflow` frame. Reconnect with `after=` / `Last-Event-ID` | 0.2.0b3 | never |
| An answer at a limit instead of a failure | `budget.on_limit: summarize`: one answer without tools at a limit; `RunResult.partial`, `limit_reached` | 0.2.0b3 | never; show a partial answer as partial |
| Why a run failed | `RunResult.error_type` (`VerificationError`, `BudgetExceededError`, ...) and `limit_reached`; workflow nodes raise typed errors. A failed conversation turn's `metadata`: `error_type: RunFailed`, `reason` (`budget_refused`, `limit_exceeded`, `timeout`, ...), `run_status`, `retryable`, `usage`, and `run_error_type` / `limit_reached` from the run; users see `ConversationPresentation.failure_messages` | 0.2.0b3 | never parse `error` or show it to users |
| A tool result too large for the context | `context_policy.oversized_tool_result: truncate` (default): offloaded when the agent can call `core.context_read`, otherwise cut and marked truncated; `fail` keeps the old failure | 0.2.0b3 | never shorten results in the host to avoid the failure |
| A larger (or smaller) context budget for one agent | `context_policy.max_context_tokens` (1,024 to 2,000,000; unset keeps the builder's 16,000); shortening, the oversized cut and compaction work against it; the model profile's `context_window_tokens` still caps what is sent. docs/context-management.md | 0.2.0b3 | never register your own `DefaultContextBuilder` only to change the budget |
| Screen retrieved evidence for injected instructions | `guardrail_policy.retrieval_injection: off \| flag \| drop`; turn it on for corpora outsiders can write to | 0.2.0b3 | never |
| Erase or export one user's data | `await container.user_data.erase_user(tenant, user, dry_run=...)` / `export_user(...)`; CLI `algen-agent-runtime users erase \| export`; `GET` / `DELETE /v1/admin/users/{user_id}/data` with `users:admin`. Custom memory stores implement `SessionDeletion.delete_session`. docs/operations.md, "Erasing or exporting one user's data" | 0.2.0b3 | your own domain records stay yours; erase them next to the Runtime's |
| Tell estimated stream charges from reported usage | declare your meter's `settle(self, ticket, usage, *, estimated: bool = False)`: the router passes `estimated=True` when a stream ended without reported usage (a meter without the keyword is called as before) | 0.2.0b3 | never |
| The agent for one turn | create the conversation with `allowed_agents=...`, then `submit(..., agent=...)` (recorded as the message's `agent`) | 0.2.0b3 | never start a new conversation only to switch agents |
| Shape a turn's run input | `submit(..., run_input=...)` keeps the user's text on the message and gives the run another input | 0.2.0b3 | never edit the stored message |
| Tool progress in a chat | `conversation_streaming.tool_steps: true`: `agent.tool.started` / `agent.tool.completed` on the conversation (names, statuses, sizes; never arguments or results); for more, read the run's events by the stored run id | 0.2.0b3 | never |

## Notes for each row

**Conversations.** `create`, `submit`, `respond`, `update`, `delete`, `feedback` and
`record_feedback` take the user and answer `NotFoundError` for another user's conversation.
`get`, `messages` and `cancel` take only the tenant: check `conversation.user_id` against the
signed-in user before calling them, as the HTTP routes do. A message's `status` is the state
machine (`accepted`, `streaming`, `awaiting_approval`, `awaiting_input`, `awaiting_credentials`,
`completed`, `failed`, `cancelled`, `expired`); do not keep another.

**Following runs.** Subscribe from the last `sequence` you have shown, so a reconnect neither
repeats nor drops events. A paused stream ends; after the answer, follow again from the last
sequence, or follow through pauses (`client.follow`, `?through_pauses=true`) to keep one
subscription for the whole run. Conversation events are the better feed for a chat UI: they carry the message ids and
coalesced deltas.

**Recovery.** Run one recovering process per database until the 0.3 leases. A streaming turn is
settled by its run's state, never replayed: followed while the run works, completed, paused
(`awaiting_approval`, `awaiting_input`, or `awaiting_credentials` when the run acted with the
caller's credentials, which are never stored), failed or cancelled as the run was; only a turn
whose run is gone fails as interrupted (retryable). Turns waiting for approval or input stay
waiting. A turn awaiting credentials continues when the user acts again: call `respond` with
`ConversationCredentialsRenewal()` and their credentials.

**Redaction.** Combine both helpers for text a person will read:
`redact(PIIRedactionPolicy().redact_value(value))`. As in the guardrail, a ten-digit number is a
phone number only with phone context, which includes a dict key such as `phone` or `mobile_no`;
`{"order": "9876543210"}` is left alone (docs/agent-tooling.md, "What the pii guardrail
redacts").

**Schema ownership.** The safest host never creates `algen_agent_runtime_*` tables: it gives the
Runtime a schema and lets `PostgresDatabase` migrate it. A host that must create them itself
applies `algen-agent-runtime migrations sql` (each release may add migrations; read the upgrade
notes) and, from 0.2.0b3, checks the result with `check_schema` in its tests and at deploy:

```python
from algen_agent_runtime.persistence import check_schema

report = await check_schema(dsn, schema="agent_runtime")
report.raise_for_issues()  # missing tables, columns, indexes or unrecorded migrations
```

Before 0.2.0b3, keep a parity test like this one, against a throwaway database:

```python
from algen_agent_runtime.persistence.postgres import PostgresDatabase

COLUMNS = (
    "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
    "WHERE table_schema = $1 AND table_name LIKE 'algen_agent_runtime_%' ORDER BY 1, 2"
)

async def test_host_schema_matches_the_runtime(dsn: str) -> None:
    reference = PostgresDatabase(dsn, schema="runtime_reference")  # the Runtime migrates it
    await reference.start()
    try:
        expected = [tuple(row) for row in await reference.fetch(COLUMNS, "runtime_reference")]
        actual = [tuple(row) for row in await reference.fetch(COLUMNS, "host_schema")]
    finally:
        await reference.close()
    assert actual == expected
```

## Capabilities by release that replace host code

From 0.2.0b3 each release's `CHANGELOG.md` entry lists these under "You can now remove". Earlier
releases, for a host upgrading from them:

- **0.1.0a14:** tool-call guards (`max_refusals_per_tool`, `repeat_guard`,
  `max_calls_per_tool`, `wrap_up_policy`, `shorten_older_tool_results`) replace prompt rules and
  host-side call counters; `UsageMeter` replaces wrapping providers to count tokens.
- **0.1.0a15:** `MeterCall` replaces passing the tenant and user to a meter some other way;
  `credentials=` and `ToolContext.credentials` replace user tokens in settings or metadata.
- **0.1.0a16:** paused conversation turns (`respond`, `pending`, `reconcile_pending_turns`)
  replace host approval state for chats and host restart follow-up.
- **0.1.0a18:** `runtime.steer` replaces steering through tool results; the approvals inbox
  (`runtime.approvals.list`, `GET /v1/approvals`) replaces host approval lists;
  `PIIRedactionPolicy().redact_value`, `redact_text` and `redact_parameters` replace host
  masking code; `storage.postgres_schema` replaces a separate database for the Runtime.
- **0.1.0a19:** attachments by reference replace inlining files in prompts.
- **0.2.0b2:** `STREAM_END_EVENT_TYPES` (with `RUN_END_EVENT_TYPES` and `RUN_PAUSE_EVENT_TYPES`)
  replaces host lists of ending and pausing events, and `client.stream_events` replaces
  host loops that stop on them; `ApprovalRequest.decided_by` replaces recording the approver in
  the host; session ownership replaces host checks that a session belongs to its user;
  `approvals:decide` and `runs:admin` replace host role checks on the HTTP API;
  `RunResult.rejected_output` replaces keeping a failed answer aside; `credentials=` on
  `AlgenAgentRuntimeClient.run`, `run_sync` and `stream` replaces calling `AgentRuntime` directly
  only to pass them.
- **0.2.0b3:** see the `CHANGELOG.md` "You can now remove" list: conversation recovery by run,
  `ConversationService.steer` and `follow`, `client.follow` through pauses, `redact_pii` /
  `redact_secrets`, `migrations sql` and `check_schema`, `UserDataService`,
  `context_policy.max_context_tokens`, `budget.on_limit: summarize` and `RunResult.error_type`.

# Conversations from a host

An application with a chat UI does not need its own conversation, message, turn or approval
state. `ConversationService` (in the Runtime since 0.1.0a1) keeps conversations and their messages
for one user each, answers every user message with an agent run in the background, streams the
answer, pauses a turn for an approval or a question and resumes it into the same message (from
0.1.0a16), and survives restarts. The HTTP API's `/v1/conversations` routes are a thin layer over
it; a host that runs the Runtime in process calls it directly.

This page is the host's view. The design is in [architecture](architecture.md#conversation-application-boundary);
message statuses, events and the HTTP routes for paused turns are in
[operations](operations.md#conversation-turns-streaming-approvals-and-input); the runnable example
is `python -m algen_agent_runtime_examples.quickstart_conversation.app`.

## Getting the service

`build_container(settings)` builds it with the built-in `runtime` handler (one agent run per turn)
and the stores the settings choose:

```python
from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.orchestration.container import build_container

container = build_container(load_settings(("agent.yaml",)))
await container.astart()          # recovery runs here (see "Restarts")
service = container.conversations
```

```yaml
storage:
  postgres_dsn: env://ALGEN_AGENT_RUNTIME_POSTGRES_DSN
  conversation_store: postgres     # memory (default) loses conversations on restart
maintenance:
  conversation_retention_days: 180 # optional: delete idle conversations, messages, events, feedback
```

## The calls

| Call | Does | Checks the owner |
| --- | --- | --- |
| `create(tenant_id=, user_id=, agent=, handler="runtime", title=, metadata=, allowed_agents=())` | A conversation owned by `user_id`. `metadata` is redacted before it is stored. `allowed_agents` (0.2.0b3) lists the other agents a turn may choose. | – |
| `list(tenant_id, user_id, limit=50, before=None)` | The user's conversations, newest first. | filters by user |
| `get(conversation_id, tenant_id)` | One conversation; `NotFoundError` if unknown. | **no** |
| `messages(conversation_id, tenant_id, limit=100, before=None, *, user_id=None)` | Its messages. | only with `user_id` (0.2.0b3) |
| `submit(conversation_id, tenant_id, user_id, text, metadata=None, *, credentials=None, attachments=(), run_input=None, agent=None)` | Stores the user message and an `accepted` assistant message, returns both, and answers in the background. `run_input` (0.2.0b3) gives the run another input while the message keeps the user's text; `agent` picks one of the conversation's `allowed_agents` for this turn. | yes |
| `respond(conversation_id, turn_id, tenant_id, user_id, response, *, credentials=None)` | Answers a turn waiting for approval, input or credentials and continues it into the same assistant message. | yes |
| `steer(conversation_id, turn_id, tenant_id, user_id, text)` | Sends more instructions to a working turn (0.2.0b3). | yes |
| `follow(conversation_id, turn_id, tenant_id, user_id)` | Re-attaches to a streaming turn no task follows (0.2.0b3). | yes |
| `cancel(conversation_id, tenant_id)` | Cancels the running answer, or ends a waiting turn and its paused run. | **no** |
| `update(conversation_id, tenant_id, user_id, *, title=, status=)` · `delete(...)` | Rename or archive; delete (cancels a running or waiting turn first and, from 0.2.0b3, deletes the conversation's memory session too). | yes |
| `record_feedback(...)` · `feedback(...)` | A user's rating of an assistant message. | yes |
| `events.subscribe(conversation_id, after=0)` | The conversation's events from sequence `after`, live. | **no** |

Calls that take a `user_id` answer `NotFoundError` for another user's conversation, exactly as for a
missing one, so ids do not leak. `get`, `messages`, `cancel` and `events` take only the tenant:
check `conversation.user_id` against the signed-in user before calling them (or pass `user_id=` to
`messages`), as the HTTP routes do
(`404` for another user). Pass the real signed-in user everywhere; a shared or hardcoded user id
makes every conversation one user's, records that user as the approver of every turn, and charges
every model call to them.

`submit` refuses (`ConflictError`) while the conversation already has a running answer or a turn
waiting for an answer; the waiting turn's id is in the error message. A waiting turn whose run
already ended is settled first, and the new message is accepted. A service that is shutting down
(`shutdown()`) refuses new turns.

## Following a turn

Every change is a `ConversationEvent` with a per-conversation `sequence`:
`conversation.message.accepted`, `assistant.message.started`, `agent.run.started`
(`{run_id, agent}`, 0.2.0b3), `assistant.message.delta` (`{message_id, delta}`, coalesced),
`assistant.message.awaiting_approval`, `awaiting_input` or `awaiting_credentials`,
`assistant.message.resumed`, and one of `completed`, `failed`, `cancelled` or `expired`. From
0.2.0b3 a steered turn adds `assistant.message.steered` and `agent.steer.delivered` /
`undelivered`, and with `conversation_streaming.tool_steps: true` tool calls add
`agent.tool.started` / `agent.tool.completed`.

```python
async for event in service.events.subscribe(conversation.id, after=last_sequence_shown):
    last_sequence_shown = event.sequence
    send_to_browser(event.model_dump(mode="json"))
```

The subscription does not end by itself: a conversation has many turns. Over HTTP,
`GET /v1/conversations/{id}/events` is server-sent events; reconnect with `Last-Event-ID` set to
the last sequence received and nothing is lost or repeated. The stored message is the record:
deltas are provisional, and the completed message replaces them. To follow the agent run behind a
turn rather than the conversation, see [following a run from a host](operations.md#following-a-run-from-a-host).

## Turns that wait for an approval or an answer

When the turn's run pauses, the assistant message becomes `awaiting_approval` or `awaiting_input`
and its `pending` field (a `ConversationPendingAction`) says what to ask: `kind` (`approval`,
`input` or `credentials`), `turn_id`, `run_id`, `approval_id`, `tool`, `proposed_action`,
`side_effect_summary`, `risk`, `expires_at`, `options`, `redacted_parameters` (never the raw
arguments), `rationale`, `parameters_schema` for a `modified` decision, or the `question` with
its suggested `choices` (0.2.0b3). Show it from the message; do not keep a copy.

- **The agent asks.** An agent with `core.ask_user` (and `user.ask` in its `tool_permissions`)
  pauses the turn as `awaiting_input` with the model's `question` and `choices`; the answer is
  free text (`ConversationInputAnswer`).
- **A delegate pauses.** When the turn's run waits for a delegated `core.subagent` run, the
  child's approval or question becomes the turn's `pending` with `child_run_id` set. Answer the
  turn as usual: the child resumes, then the parent, into the same message.
- **Credentials after a restart.** A run that acted with the caller's credentials is paused by
  recovery in `awaiting_credentials` (credentials are never stored). Its turn becomes
  `awaiting_credentials` with `pending.kind: credentials`; when the user acts again, continue it
  with `respond(..., ConversationCredentialsRenewal(), credentials=...)` or
  `POST /v1/conversations/{id}/turns/{turn_id}/credentials` with the headers
  `api.credential_headers` lists. Without credentials the call is refused (`ConflictError`, 409)
  and the turn keeps waiting. The maintenance sweep expires it after
  `clarification_timeout_seconds`, like a question.

```python
from algen_agent_runtime.conversations import (
    ConversationApprovalDecision,
    ConversationCredentialsRenewal,
    ConversationInputAnswer,
)

await service.respond(
    conversation.id,
    pending.turn_id,
    tenant_id,
    signed_in_user,
    ConversationApprovalDecision(decision="approved"),  # or "rejected" (with reason), "modified"
    credentials={"authorization": request_headers["authorization"]},
)
await service.respond(conversation.id, pending.turn_id, tenant_id, signed_in_user,
                      ConversationInputAnswer(text="The Berlin office"))
await service.respond(conversation.id, pending.turn_id, tenant_id, signed_in_user,
                      ConversationCredentialsRenewal(), credentials=caller_credentials)
```

- **Who decided.** The turn's owner is the one who answers, and the run's approval records them
  as `decided_by` (from 0.2.0b2, with `decided_at`); `assistant.message.resumed` carries
  `decided_by` too.
- **One answer.** A second answer, an answer of the wrong kind, or one to a turn that no longer
  waits is `ConflictError`. An answer after the approval expired ends the turn as `expired` and
  raises `PendingTurnExpiredError` (a `ConflictError`).
- **Other approvers.** An approver who is not the owner decides through the approvals inbox
  (`runtime.approvals.list(...)`, `GET /v1/approvals`; deciding another user's run over HTTP needs
  `approvals:decide` from 0.2.0b2); see [operations](operations.md#approval-inbox). In process,
  `runtime.resume(run_id, tenant_id, {"decision": ...}, decided_by=approver)` does not check who may
  decide: the host checks its own roles first.
- **Cancelling.** `cancel` ends a waiting turn and cancels its paused run.

## Steering, agents, input and tool steps

`steer(conversation_id, turn_id, tenant_id, user_id, text)` or
`POST /v1/conversations/{id}/turns/{turn_id}/steer` (0.2.0b3) sends more instructions to a working
turn (`assistant.message.steered`, then `agent.steer.delivered` or `undelivered`, ids and sizes
only). A conversation created with `allowed_agents` lets `submit(..., agent=...)` pick the turn's
agent (recorded as the message's `agent`). `submit(..., run_input=...)` shapes the run's input
while keeping the user's text. `messages(..., user_id=...)` checks the owner.

`conversation_streaming.tool_steps: true` adds `agent.tool.started` / `agent.tool.completed`
(names, statuses, argument names and result sizes, never arguments or results); for more, read
the run's own events by the stored run id.

## Failures

A failed turn's `metadata` has `error_type: RunFailed`, `reason` (e.g. `budget_refused`,
`limit_exceeded`, `timeout`), `run_status`, `retryable` and `usage`, and, when the run reports
them (0.2.0b3), `run_error_type` (the run's `RunResult.error_type`, such as `BudgetExceededError`
or `MeterRefusedError`) and `limit_reached`. Check `error_type` for every failed turn and
`run_error_type` for the cause. Users see a fixed text per reason from
`ConversationPresentation.failure_messages`, never the run's error.

## Restarts

With `runtime.recover_incomplete_runs: true` (the default), `container.astart()` runs
`Runtime.recover()` and then `service.recover()`; keep that order if you call them yourself, so
recovered runs are driven by this process before their turns are followed.

Each turn stores its run id on the assistant message (`run_ids`) as soon as the run starts,
announced by `agent.run.started` `{run_id, agent}` (0.2.0b3). For each turn left streaming,
recovery finds its run (the stored id, or the run store's `find_by_turn` when the process stopped
before storing it) and never starts the work again:

- a working run is followed into the same message;
- a run that completed meanwhile completes the turn with its answer;
- a paused run moves the turn to `awaiting_approval`, `awaiting_input` or `awaiting_credentials`
  with its `pending` action, or to `expired` if the approval expired;
- a failed run fails the turn with its reason, a cancelled one cancels it;
- only a turn with no run, or whose run is gone, fails as interrupted (`metadata.error_type:
  InterruptedResponse`, `retryable: true`). It is never replayed, since its tools may already
  have acted; let the user retry.

Settling is a conditional update, so repeated or concurrent recovery settles a turn once.
`follow(conversation_id, turn_id, tenant_id, user_id)` re-attaches to one streaming turn.
`shutdown()` detaches turns that outlive its grace period instead of cancelling them, so a deploy
does not lose answers; a user's cancel still cancels. A turn waiting for approval or input stays
waiting: its pending action is on the message and its paused run in the run store.
- `reconcile_pending_turns()` (from 0.1.0a16) handles the turns that moved on while the process was
  down: a run that was resumed but whose message was not updated is followed to its end (never run
  again), and a run that ended while its turn waited (expired, cancelled, failed) ends the turn
  with that outcome. The maintenance sweep (`maintenance.enabled`, or
  `algen-agent-runtime maintenance`) calls it on every pass while `maintenance.expire_paused_runs`
  is on (the default), so a host never needs its own follow-up loop.

Recovery is single-node, and the "one active turn per conversation" check is per process: enable
`recover_incomplete_runs` on one process per database until the 0.3 leases
([operations](operations.md#restart-recovery)).

## Credentials and files

`submit(..., credentials=...)` (from 0.1.0a16) and `respond(..., credentials=...)` hand the
caller's credentials to the turn's run, for tools that act as the user
([tool authoring](tool-authoring.md#credentials-of-the-caller)). They are held in memory for that
turn only, never stored, emitted, audited or logged; a turn that pauses drops them, so pass them
again with the answer. Over HTTP, `api.credential_headers` (0.2.0b3) lists the request headers to
forward to tools as the caller's credentials; none are forwarded by default. A run that was acting
with credentials when the process stopped waits for them again (see "Credentials after a restart"
above), unless the container has a `recovery_credentials` provider.

`submit(..., attachments=(RunAttachment(artifact_id=..., media_type=...),))` (from 0.1.0a19)
attaches uploaded files by reference; the message keeps them as `file` blocks and the agent must
accept attachments ([attachments](attachments.md)).

## Sessions

The `runtime` handler runs each turn with `session_id` set to the conversation id and `user_id` to
its owner, so the conversation's memory is the session's. From 0.2.0b2 a session belongs to its
first user: a run for another user that names it is refused. From 0.2.0b3 `delete` also deletes
that memory session and its owner (`RuntimeConversationHandler.forget_conversation`); a custom
memory store implements `SessionDeletion.delete_session` to delete the owner with it. A host that starts its own runs gives
each user their own `session_id` rather than sharing one.

## Custom handlers

A turn that is not one agent run (a workflow, a router to several agents) is a handler: an object
with a `name` and `handle(conversation, user_message, history, emit)` returning a
`ConversationTurnResult`, registered with `service.handlers.register(handler)` and named in
`create(handler=...)`. To pause and resume, implement `ResumableConversationHandler` (`resume`,
`follow`, `settle`, `pending_state`, `cancel_pending`); to be recovered by run after a restart,
implement `ReattachableConversationHandler` (`turn_run`, `follow_turn`, 0.2.0b3); to take
credentials, accept a `credentials` keyword in `handle`. A handler with
`forget_conversation(conversation)` is called when a conversation is deleted.

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Coroutine,
    Mapping,
    Sequence,
)
from datetime import datetime, timedelta
from typing import Any, Literal

import structlog
from opentelemetry.context import Context

from algen_agent_runtime.approvals.service import ApprovalStatus
from algen_agent_runtime.conversations.contracts import (
    AWAITING_MESSAGE_STATUSES,
    Conversation,
    ConversationApprovalDecision,
    ConversationCredentialsRenewal,
    ConversationEvent,
    ConversationEventPublisher,
    ConversationHandler,
    ConversationInputAnswer,
    ConversationMessage,
    ConversationPendingAction,
    ConversationStatus,
    ConversationStore,
    ConversationTurnInterruptedError,
    ConversationTurnResponse,
    ConversationTurnResult,
    MessageStatus,
    NoticeBlock,
    PendingTurnExpiredError,
)
from algen_agent_runtime.conversations.feedback import (
    ConversationFeedback,
    ConversationFeedbackStore,
    FeedbackRating,
    InMemoryConversationFeedbackStore,
    record_feedback,
)
from algen_agent_runtime.conversations.followups import FollowupSuggestionProvider
from algen_agent_runtime.conversations.presentation import ConversationPresentation
from algen_agent_runtime.conversations.streaming import (
    DELTA_EVENT,
    DETACH_SIGNAL,
    RUN_STARTED_EVENT,
    DeltaCoalescer,
    detaching,
    follow_run,
)
from algen_agent_runtime.events.contracts import AuditEvent
from algen_agent_runtime.exceptions.errors import (
    ConfigurationError,
    ConflictError,
    NotFoundError,
)
from algen_agent_runtime.observability.traccia_adapter import (
    NoopObservabilityAdapter,
    ObservabilityAdapter,
)
from algen_agent_runtime.security.redaction import redact
from algen_agent_runtime.tools.contracts import read_only_credentials
from algen_agent_runtime.types.contracts import (
    PAUSED_STATUSES,
    TERMINAL_STATUSES,
    FileBlock,
    Role,
    RunAttachment,
    RunRequest,
    RunResult,
    RunState,
    RunStatus,
    SteeringMessage,
    TextBlock,
    utc_now,
)
from algen_agent_runtime.types.interfaces import Runtime, SessionDeletion


class ConversationHandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, ConversationHandler] = {}

    def register(self, handler: ConversationHandler) -> None:
        if handler.name in self._handlers:
            raise ConflictError(f"conversation handler {handler.name!r} is already registered")
        self._handlers[handler.name] = handler

    def get(self, name: str) -> ConversationHandler:
        try:
            return self._handlers[name]
        except KeyError as exc:
            raise NotFoundError(f"conversation handler {name!r} is not registered") from exc

    def list(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))


class RuntimeConversationHandler:
    """Answer each turn with one agent run, streamed, that may pause for approval or input.

    When ``stream`` is on and every model the agent may use can stream, the run streams and its
    text deltas reach the conversation as ``assistant.message.delta`` events, coalesced to at most
    one per ``delta_interval_seconds`` (or per ``delta_max_chars`` characters). Otherwise the turn
    produces only its final message, as before.

    With ``tool_steps``, the run's tool calls also reach the conversation, as
    ``agent.tool.started`` and ``agent.tool.completed`` events carrying the tool's name, status,
    side effect, argument names and result size: never arguments or results.

    Each turn announces its run with an ``agent.run.started`` event (``{"run_id": ...}``), which
    the service stores on the assistant message, so a restarted service follows the run with
    :meth:`follow_turn` instead of failing the turn.
    """

    name = "runtime"

    def __init__(
        self,
        runtime: Runtime,
        presentation: ConversationPresentation | None = None,
        *,
        stream: bool = True,
        delta_interval_seconds: float = 0.05,
        delta_max_chars: int = 2_048,
        tool_steps: bool = False,
    ) -> None:
        self._runtime = runtime
        self._tool_steps = tool_steps
        self._presentation = presentation or ConversationPresentation()
        self._stream = stream
        self._delta_interval = delta_interval_seconds
        self._delta_max_chars = delta_max_chars
        # The run's last event sequence just before each resume, so ``follow`` forwards every
        # delta of the resumed drive even if it starts before ``follow`` subscribes.
        self._resume_cursors: dict[str, int] = {}

    async def forget_conversation(self, conversation: Conversation) -> None:
        """Delete the memory session the conversation's runs used (its id), owner included."""
        memory = getattr(self._runtime, "memory", None)
        if memory is None:
            return
        if isinstance(memory, SessionDeletion):
            await memory.delete_session(conversation.tenant_id, conversation.id)
        else:
            await memory.delete(conversation.tenant_id, conversation.id)

    async def handle(
        self,
        conversation: Conversation,
        user_message: ConversationMessage,
        history: Sequence[ConversationMessage],
        emit: Any,
        *,
        credentials: Mapping[str, str] | None = None,
        run_input: str | None = None,
    ) -> ConversationTurnResult:
        """Answer the turn with one run of the conversation's agent.

        ``run_input`` is the text the run gets instead of the message's text (see
        :meth:`ConversationService.submit`).
        """
        await emit(
            "workflow.progress",
            self._presentation.progress(
                {
                    "step": "respond",
                    "label": "Preparing your answer",
                    "description": "Reviewing your request and preparing a response.",
                    "index": 1,
                    "status": "started",
                    "safe_summary": True,
                },
                {
                    "step": "runtime_execution",
                    "label": "Running the selected agent",
                    "description": (
                        f"Invoking runtime agent {conversation.agent!r} for this conversation turn."
                    ),
                    "index": 1,
                    "status": "started",
                    "safe_summary": True,
                },
            ),
        )
        await emit("agent.status.changed", {"status": "running", "agent": conversation.agent})
        stream = await self._streams(conversation.agent)
        state = await self._runtime.start(
            RunRequest(
                agent=conversation.agent,
                input=user_message.text_content if run_input is None else run_input,
                attachments=tuple(
                    RunAttachment(artifact_id=block.artifact_id, media_type=block.media_type)
                    for block in user_message.content
                    if isinstance(block, FileBlock)
                ),
                session_id=conversation.id,
                conversation_id=conversation.id,
                turn_id=user_message.id,
                tenant_id=conversation.tenant_id,
                user_id=conversation.user_id,
                stream=stream,
                metadata={"interface": "conversation"},
            ),
            credentials=credentials,
        )
        await emit(RUN_STARTED_EVENT, {"run_id": state.id, "agent": conversation.agent})
        result = await follow_run(
            self._runtime,
            state.id,
            conversation.tenant_id,
            self._coalescer(emit) if stream else None,
            steps=emit,
            tool_steps=self._tool_steps,
        )
        return await self._turn_result(conversation, user_message.id, result, paused=False)

    async def resume(
        self,
        conversation: Conversation,
        pending: ConversationPendingAction,
        response: ConversationTurnResponse,
        *,
        credentials: Mapping[str, str] | None = None,
    ) -> None:
        run_id = self._run_id(pending)
        payload: dict[str, Any]
        if pending.kind == "approval":
            if not isinstance(response, ConversationApprovalDecision):
                raise ConflictError(f"turn {pending.turn_id!r} awaits an approval decision")
            if pending.options and response.decision not in pending.options:
                raise ConflictError(
                    f"turn {pending.turn_id!r} accepts only: {', '.join(pending.options)}"
                )
            if await self._approval_expired(conversation, pending):
                raise PendingTurnExpiredError(f"turn {pending.turn_id!r} approval expired")
            payload = {"decision": response.decision}
            if response.modified_parameters is not None:
                payload["modified_parameters"] = response.modified_parameters
            if response.reason:
                payload["reason"] = response.reason
        elif pending.kind == "credentials":
            if not isinstance(response, ConversationCredentialsRenewal):
                raise ConflictError(f"turn {pending.turn_id!r} awaits the caller's credentials")
            if not credentials:
                raise ConflictError(
                    f"turn {pending.turn_id!r} awaits the caller's credentials: pass them again"
                )
            payload = {}
        else:
            if not isinstance(response, ConversationInputAnswer):
                raise ConflictError(f"turn {pending.turn_id!r} awaits an answer, not a decision")
            payload = {"clarification": response.text}
        cursor = await self._event_cursor(run_id)
        # Only the conversation's owner may answer its turns, so they decided the approval. A
        # Runtime written before 0.2, without decided_by, is called as before.
        attribution = (
            {"decided_by": conversation.user_id}
            if pending.kind == "approval" and _accepts(self._runtime.resume, "decided_by")
            else {}
        )
        await self._runtime.resume(
            run_id, conversation.tenant_id, payload, credentials=credentials, **attribution
        )
        self._resume_cursors[run_id] = cursor

    async def follow(
        self,
        conversation: Conversation,
        pending: ConversationPendingAction,
        emit: Any,
    ) -> ConversationTurnResult:
        run_id = self._run_id(pending)
        cursor = self._resume_cursors.pop(run_id, None)
        if cursor is None:
            # Followed after a restart: forward only what the run emits from now on.
            cursor = await self._event_cursor(run_id)
        state = await self._run_state(run_id, conversation.tenant_id)
        stream = bool(state is not None and state.request.stream)
        result = await follow_run(
            self._runtime,
            run_id,
            conversation.tenant_id,
            self._coalescer(emit) if stream else None,
            after=cursor,
            steps=emit,
            tool_steps=self._tool_steps,
        )
        return await self._turn_result(conversation, pending.turn_id, result, paused=False)

    async def follow_turn(
        self,
        conversation: Conversation,
        turn_id: str,
        run_id: str,
        emit: Any,
    ) -> ConversationTurnResult:
        """Follow the run of a turn that was streaming when its process stopped.

        It never starts the work again. A run this process drives (``Runtime.recover`` resumed
        it) is followed to its end or pause, forwarding what it emits from now on. A run that
        already ended or paused settles at once: completed with its answer, paused with its
        pending action (``expired`` if its approval expired meanwhile), failed or cancelled with
        the run's own outcome. A run another process drives is polled until it ends or pauses.
        Raises :class:`ConversationTurnInterruptedError` when the run is gone, belongs to another
        conversation, or nothing drives it past its deadline.
        """
        tenant_id = conversation.tenant_id
        state = await self._run_state(run_id, tenant_id)
        if state is None or state.request.conversation_id not in (None, conversation.id):
            raise ConversationTurnInterruptedError(f"turn {turn_id!r} has no run to follow")
        cursor = await self._event_cursor(run_id)
        try:
            result = await follow_run(
                self._runtime,
                run_id,
                tenant_id,
                self._coalescer(emit) if state.request.stream else None,
                after=cursor,
                steps=emit,
                tool_steps=self._tool_steps,
            )
            while result.status not in TERMINAL_STATUSES | PAUSED_STATUSES:
                # Not driven here: another process drives it, or nothing does any more.
                current = await self._run_state(run_id, tenant_id)
                if current is None:
                    raise ConversationTurnInterruptedError(f"turn {turn_id!r} lost its run")
                if current.status not in TERMINAL_STATUSES | PAUSED_STATUSES:
                    limit = (current.deadline or current.updated_at) + _ORPHAN_GRACE
                    if utc_now() > limit:
                        raise ConversationTurnInterruptedError(
                            f"turn {turn_id!r}: nothing drives its run any more"
                        )
                    await asyncio.sleep(_ORPHAN_POLL_SECONDS)
                result = await self._runtime.wait(run_id, tenant_id)
        except NotFoundError as exc:
            raise ConversationTurnInterruptedError(f"turn {turn_id!r} lost its run") from exc
        turn = await self._turn_result(conversation, turn_id, result, paused=False)
        if turn.pending is not None and await self._approval_expired(conversation, turn.pending):
            return ConversationTurnResult(
                content=(TextBlock(text=_EXPIRED_TEXT),),
                run_ids=turn.run_ids,
                outcome="expired",
                metadata={"reason": "approval_expired"},
            )
        return turn

    async def steer(self, conversation: Conversation, run_id: str, text: str) -> SteeringMessage:
        """Queue the user's ``text`` for the turn's run (``Runtime.steer``)."""
        steer = getattr(self._runtime, "steer", None)
        if steer is None:
            raise ConflictError("this runtime cannot steer runs")
        message: SteeringMessage = await steer(run_id, conversation.tenant_id, text)
        return message

    async def turn_run(self, conversation: Conversation, turn_id: str) -> str | None:
        """The run started for ``turn_id``, found in the run store by conversation and turn.

        Used for a streaming turn whose message has no run id: a process that stopped between
        starting the run and storing its id, or a message an earlier release stored.
        """
        runs = getattr(self._runtime, "runs", None)
        finder = getattr(runs, "find_by_turn", None)
        if finder is None:
            return None
        state: RunState | None = await finder(conversation.tenant_id, conversation.id, turn_id)
        return state.id if state is not None else None

    async def _approval_expired(
        self, conversation: Conversation, pending: ConversationPendingAction
    ) -> bool:
        """Whether a pending approval's request has already expired."""
        approvals = getattr(self._runtime, "approvals", None)
        if pending.kind != "approval" or approvals is None or not pending.approval_id:
            return False
        approval = await approvals.get(pending.approval_id, conversation.tenant_id)
        return approval is not None and (
            approval.status == ApprovalStatus.EXPIRED
            or (approval.status == ApprovalStatus.PENDING and approval.expires_at <= utc_now())
        )

    async def settle(
        self, conversation: Conversation, pending: ConversationPendingAction
    ) -> ConversationTurnResult:
        """The result of a pending turn whose run ended while the turn waited."""
        result = await self._runtime.wait(self._run_id(pending), conversation.tenant_id)
        return await self._turn_result(conversation, pending.turn_id, result, paused=True)

    async def pending_state(
        self, conversation: Conversation, pending: ConversationPendingAction
    ) -> Literal["paused", "running", "ended"]:
        state = await self._run_state(self._run_id(pending), conversation.tenant_id)
        if state is None or state.status in TERMINAL_STATUSES:
            return "ended"
        if state.status in PAUSED_STATUSES:
            return "paused"
        return "running"

    async def cancel_pending(
        self, conversation: Conversation, pending: ConversationPendingAction
    ) -> None:
        await self._runtime.cancel(self._run_id(pending), conversation.tenant_id)

    def _coalescer(self, emit: Any) -> DeltaCoalescer:
        return DeltaCoalescer(
            emit, interval_seconds=self._delta_interval, max_chars=self._delta_max_chars
        )

    async def _streams(self, agent_name: str) -> bool:
        """Stream only when asked to and every model the agent may use can stream."""
        if not self._stream or getattr(self._runtime, "events", None) is None:
            return False
        agents = getattr(self._runtime, "agents", None)
        router = getattr(self._runtime, "router", None)
        supports = getattr(router, "supports_streaming", None)
        if agents is None or supports is None:
            return False
        try:
            agent = agents.get(agent_name)
        except NotFoundError:
            return False  # start reports the unknown agent
        profiles = (agent.default_model, *agent.fallback_models)
        return bool(await supports(profiles, agent.model_allowlist))

    @staticmethod
    def _run_id(pending: ConversationPendingAction) -> str:
        if not pending.run_id:
            raise ConflictError(f"turn {pending.turn_id!r} has no paused run")
        return pending.run_id

    async def _run_state(self, run_id: str, tenant_id: str) -> RunState | None:
        status = getattr(self._runtime, "status", None)
        if status is None:
            return None
        try:
            state: RunState = await status(run_id, tenant_id)
        except NotFoundError:
            return None
        return state

    async def _event_cursor(self, run_id: str) -> int:
        """The run's latest event sequence, so a resumed turn forwards only newer deltas."""
        events = getattr(self._runtime, "events", None)
        if events is None:
            return 0
        return int(await events.next_sequence(run_id)) - 1

    async def _end_reason(self, run_id: str) -> str | None:
        events = getattr(self._runtime, "events", None)
        if events is None:
            return None
        for event in reversed(tuple(await events.history(run_id))):
            if event.type == "run.failed":
                reason = event.data.get("reason")
                return str(reason) if reason else None
        return None

    async def _turn_result(
        self,
        conversation: Conversation,
        turn_id: str,
        result: RunResult,
        *,
        paused: bool,
    ) -> ConversationTurnResult:
        run_ids = (result.run_id,)
        if result.status in PAUSED_STATUSES:
            pending = await self._pending(conversation, turn_id, result.run_id)
            content: tuple[Any, ...]
            if pending.kind == "approval":
                content = (
                    NoticeBlock(
                        level="info",
                        text=f"Waiting for approval: {pending.proposed_action or 'proposed action'}",
                    ),
                )
                return ConversationTurnResult(
                    content=content, run_ids=run_ids, outcome="awaiting_approval", pending=pending
                )
            if pending.kind == "credentials":
                content = (NoticeBlock(level="info", text=_CREDENTIALS_TEXT),)
                return ConversationTurnResult(
                    content=content,
                    run_ids=run_ids,
                    outcome="awaiting_credentials",
                    pending=pending,
                )
            content = (TextBlock(text=pending.question or "Please provide more detail."),)
            return ConversationTurnResult(
                content=content, run_ids=run_ids, outcome="awaiting_input", pending=pending
            )
        if result.status == RunStatus.CANCELLED:
            return ConversationTurnResult(
                content=(TextBlock(text="Response cancelled."),),
                run_ids=run_ids,
                outcome="cancelled",
            )
        if result.status in {RunStatus.TIMED_OUT, RunStatus.FAILED}:
            reason = await self._end_reason(result.run_id)
            if result.status == RunStatus.TIMED_OUT and (paused or reason in _EXPIRY_REASONS):
                return ConversationTurnResult(
                    content=(TextBlock(text=_EXPIRED_TEXT),),
                    run_ids=run_ids,
                    outcome="expired",
                    metadata={"reason": reason or "expired"},
                )
            if reason in _REJECTION_REASONS:
                # Written by the Runtime and safe to show: the user declined the action.
                return ConversationTurnResult(
                    content=(TextBlock(text=result.error or "The proposed action was rejected."),),
                    run_ids=run_ids,
                    outcome="failed",
                    metadata={"reason": reason},
                )
            return self._failed(result, reason)
        if result.error:
            raise RuntimeError(result.error)
        return ConversationTurnResult(
            content=(TextBlock(text=result.output or ""),),
            run_ids=run_ids,
            artifacts=result.artifacts,
            citations=tuple(item.model_dump(mode="json") for item in result.citations),
        )

    def _failed(self, result: RunResult, reason: str | None) -> ConversationTurnResult:
        """A failed turn that keeps why its run failed: the reason, status and usage in the
        metadata, and a user-safe text for the reason (never the run's error text)."""
        if reason is None and result.status == RunStatus.TIMED_OUT:
            reason = "timeout"
        reason = reason or "run_failed"
        metadata: dict[str, Any] = {
            "error_type": "RunFailed",
            "reason": reason,
            "run_status": result.status.value,
            "retryable": reason in _RETRYABLE_REASONS,
            "usage": result.execution_summary.usage.model_dump(mode="json"),
        }
        # The run's own typed error and the limit it reached (RunResult, 0.2.0b3), when set:
        # error_type stays "RunFailed" for every failed turn, so hosts keep one check.
        if result.error_type:
            metadata["run_error_type"] = result.error_type
        if result.limit_reached:
            metadata["limit_reached"] = result.limit_reached
        return ConversationTurnResult(
            content=(TextBlock(text=self._presentation.run_failure_message(reason, result.error)),),
            run_ids=(result.run_id,),
            outcome="failed",
            metadata=metadata,
        )

    async def _pending(
        self, conversation: Conversation, turn_id: str, run_id: str
    ) -> ConversationPendingAction:
        state = await self._run_state(run_id, conversation.tenant_id)
        payload: dict[str, Any] = dict((state.pause_payload if state else None) or {})
        paused_at = _parse_time(payload.get("paused_at")) or utc_now()
        child_run_id = _optional_text(payload.get("child_run_id"))
        if state is not None and state.status == RunStatus.AWAITING_CREDENTIALS:
            # A run recovered without the caller's credentials it was acting with: nothing to
            # decide or answer, the user continues it by passing their credentials again.
            return ConversationPendingAction(
                kind="credentials",
                turn_id=turn_id,
                run_id=run_id,
                child_run_id=child_run_id,
                paused_at=paused_at,
            )
        if state is not None and state.status == RunStatus.AWAITING_CLARIFICATION:
            choices = payload.get("choices")
            return ConversationPendingAction(
                kind="input",
                turn_id=turn_id,
                run_id=run_id,
                question=_optional_text(payload.get("question")),
                choices=tuple(str(item) for item in choices) if isinstance(choices, list) else (),
                child_run_id=child_run_id,
                paused_at=paused_at,
            )
        call_id = str(payload.get("tool_call_id") or "")
        calls = state.pending_tool_calls if state is not None else []
        tool_name = next((call.name for call in calls if call.id == call_id), None)
        if tool_name is None:
            # A child run's approval (core.subagent): the call is the child's, named in the payload.
            tool_name = _optional_text(payload.get("tool"))
        schema: dict[str, Any] | None = None
        tools = getattr(self._runtime, "tools", None)
        if tool_name and tools is not None:
            try:
                schema = dict(tools.get(tool_name).definition.input_schema)
            except NotFoundError:
                schema = None
        redacted = payload.get("redacted_parameters")
        return ConversationPendingAction(
            kind="approval",
            turn_id=turn_id,
            run_id=run_id,
            approval_id=_optional_text(payload.get("approval_id")),
            approval_kind=_optional_text(payload.get("kind")),
            tool=tool_name,
            proposed_action=_optional_text(payload.get("proposed_action")),
            side_effect_summary=_optional_text(payload.get("side_effect_summary")),
            risk=_optional_text(payload.get("risk")),
            expires_at=_parse_time(payload.get("expires_at")),
            options=tuple(str(item) for item in payload.get("options") or ()),
            redacted_parameters=dict(redacted) if isinstance(redacted, Mapping) else {},
            rationale=_optional_text(payload.get("rationale")),
            parameters_schema=schema,
            child_run_id=child_run_id,
            paused_at=paused_at,
        )


_EXPIRY_REASONS = frozenset({"approval_expired", "clarification_expired", "credentials_expired"})
_REJECTION_REASONS = frozenset({"approval_rejected", "continuation_rejected"})
_RETRYABLE_REASONS = frozenset({"timeout", "timeout_during_recovery"})
_EXPIRED_TEXT = "This request expired before it was answered. Please ask again."
_CREDENTIALS_TEXT = (
    "This response was paused during a service restart. Continue it to pick up where it left off."
)
_INTERRUPTED_TEXT = "The response was interrupted during a service restart. Please retry."
# A run nothing drives is given up this long after its deadline; it is polled meanwhile.
_ORPHAN_GRACE = timedelta(seconds=30)
_ORPHAN_POLL_SECONDS = 0.25


def _optional_text(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


_TurnWork = Callable[
    [Callable[[str, dict[str, Any] | None], Awaitable[None]], Sequence[ConversationMessage]],
    Awaitable[ConversationTurnResult],
]

_STATUS_BY_OUTCOME: dict[str, MessageStatus] = {
    "failed": MessageStatus.FAILED,
    "cancelled": MessageStatus.CANCELLED,
    "expired": MessageStatus.EXPIRED,
    "awaiting_approval": MessageStatus.AWAITING_APPROVAL,
    "awaiting_input": MessageStatus.AWAITING_INPUT,
    "awaiting_credentials": MessageStatus.AWAITING_CREDENTIALS,
}
_EVENT_BY_STATUS: dict[MessageStatus, str] = {
    MessageStatus.COMPLETED: "assistant.message.completed",
    MessageStatus.FAILED: "assistant.message.failed",
    MessageStatus.CANCELLED: "assistant.message.cancelled",
    MessageStatus.EXPIRED: "assistant.message.expired",
    MessageStatus.AWAITING_APPROVAL: "assistant.message.awaiting_approval",
    MessageStatus.AWAITING_INPUT: "assistant.message.awaiting_input",
    MessageStatus.AWAITING_CREDENTIALS: "assistant.message.awaiting_credentials",
}


def _output_text(result: ConversationTurnResult) -> str:
    return "\n".join(
        str(getattr(block, "text", "")) for block in result.content if getattr(block, "text", None)
    )


def _accepts(function: Any, keyword: str) -> bool:
    """Whether ``function`` takes the keyword argument ``keyword`` (or any keyword)."""
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == keyword or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


_IN_PROGRESS = frozenset({MessageStatus.ACCEPTED, MessageStatus.STREAMING})


def _for_turn(conversation: Conversation, assistant: ConversationMessage) -> Conversation:
    """The conversation as the handler sees it for one turn: with the turn's agent."""
    if assistant.agent is None or assistant.agent == conversation.agent:
        return conversation
    return conversation.model_copy(update={"agent": assistant.agent})


_PUBLISH_ATTEMPTS = 8


class _KeyedLocks:
    """An ``asyncio.Lock`` per key, kept only while someone holds or waits for it."""

    def __init__(self) -> None:
        self._locks: dict[str, tuple[asyncio.Lock, list[int]]] = {}

    def __len__(self) -> int:
        return len(self._locks)

    @contextlib.asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        entry = self._locks.get(key)
        if entry is None:
            entry = self._locks[key] = (asyncio.Lock(), [0])
        lock, users = entry
        users[0] += 1
        try:
            async with lock:
                yield
        finally:
            users[0] -= 1
            if users[0] == 0:
                self._locks.pop(key, None)


def _awaited(status: MessageStatus) -> str:
    if status == MessageStatus.AWAITING_APPROVAL:
        return "approval"
    if status == MessageStatus.AWAITING_CREDENTIALS:
        return "credentials"
    return "input"


class ConversationService:
    def __init__(
        self,
        store: ConversationStore,
        events: ConversationEventPublisher,
        handlers: ConversationHandlerRegistry,
        audits: Any | None = None,
        observability: ObservabilityAdapter | None = None,
        telemetry_include_content: bool = False,
        telemetry_max_content_chars: int = 16_384,
        presentation: ConversationPresentation | None = None,
        feedback_store: ConversationFeedbackStore | None = None,
        followup_provider: FollowupSuggestionProvider | None = None,
    ) -> None:
        self.store = store
        self.events = events
        self.handlers = handlers
        self._audits = audits
        self._observability = observability or NoopObservabilityAdapter()
        self._telemetry_include_content = telemetry_include_content
        self._telemetry_max_content_chars = telemetry_max_content_chars
        self.presentation = presentation or ConversationPresentation()
        self.feedback_store = feedback_store or InMemoryConversationFeedbackStore()
        self._followup_provider = followup_provider
        self._tracer = self._observability.tracer("algen_agent_runtime.conversations")
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # One lock per conversation, so turns of different conversations never wait on each
        # other; a lock exists only while held or awaited.
        self._locks = _KeyedLocks()
        self._emit_locks = _KeyedLocks()
        self._detach = asyncio.Event()
        self._accepting = True

    async def create(
        self,
        *,
        tenant_id: str,
        user_id: str,
        agent: str,
        handler: str = "runtime",
        title: str = "New conversation",
        metadata: dict[str, Any] | None = None,
        allowed_agents: Sequence[str] = (),
    ) -> Conversation:
        """Create a conversation answered by ``agent``.

        ``allowed_agents`` are the other agents a turn may choose (``submit(..., agent=...)``);
        by default every turn is answered by ``agent``.
        """
        self.handlers.get(handler)
        conversation = Conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            agent=agent,
            allowed_agents=tuple(dict.fromkeys(item for item in allowed_agents if item != agent)),
            handler=handler,
            title=title,
            metadata=redact(metadata or {}),
        )
        await self.store.create(conversation)
        await self._emit(conversation, "conversation.created", {"title": title})
        return conversation

    async def get(self, conversation_id: str, tenant_id: str) -> Conversation:
        conversation = await self.store.get(conversation_id, tenant_id)
        if conversation is None:
            raise NotFoundError(f"conversation {conversation_id!r} not found")
        return conversation

    async def list(
        self,
        tenant_id: str,
        user_id: str,
        limit: int = 50,
        before: datetime | None = None,
    ) -> Sequence[Conversation]:
        return await self.store.list(tenant_id, user_id, limit, before)

    async def delete(self, conversation_id: str, tenant_id: str, user_id: str) -> None:
        conversation = await self.get(conversation_id, tenant_id)
        if conversation.user_id != user_id:
            raise NotFoundError(f"conversation {conversation_id!r} not found")
        task = self._tasks.get(conversation_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._end_pending_run(conversation)
        if not await self.store.delete(conversation_id, tenant_id):
            raise NotFoundError(f"conversation {conversation_id!r} not found")
        # The session memory holds the turns' tool results: it goes with the conversation.
        try:
            handler: Any = self.handlers.get(conversation.handler)
        except NotFoundError:
            handler = None
        forget = getattr(handler, "forget_conversation", None)
        if callable(forget):
            await forget(conversation)
        if self._audits is not None:
            await self._audits.append(
                AuditEvent(
                    action="conversation.deleted",
                    outcome="ok",
                    tenant_id=tenant_id,
                    actor_id=user_id,
                    resource_id=conversation_id,
                    metadata={"title": conversation.title},
                )
            )

    async def update(
        self,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
        *,
        title: str | None = None,
        status: ConversationStatus | None = None,
    ) -> Conversation:
        conversation = await self.get(conversation_id, tenant_id)
        if conversation.user_id != user_id:
            raise NotFoundError(f"conversation {conversation_id!r} not found")
        expected = conversation.version
        if title is not None:
            conversation.title = title.strip()
        if status is not None:
            conversation.status = status
        conversation.updated_at = utc_now()
        await self.store.save(conversation, expected)
        await self._emit(
            conversation,
            "conversation.updated",
            {"title": conversation.title, "status": conversation.status.value},
        )
        return conversation

    async def suggestions(
        self, handler: str, tenant_id: str, user_id: str, limit: int = 5
    ) -> tuple[str, ...]:
        selected = self.handlers.get(handler)
        provider = getattr(selected, "suggestions", None)
        if provider is None:
            return ()
        values = await provider(tenant_id, user_id, limit)
        return tuple(str(value) for value in values[:limit])

    async def messages(
        self,
        conversation_id: str,
        tenant_id: str,
        limit: int = 100,
        before: datetime | None = None,
        *,
        user_id: str | None = None,
    ) -> Sequence[ConversationMessage]:
        """The conversation's messages, oldest first.

        With ``user_id``, only the conversation's owner sees them: another user gets
        ``NotFoundError``, as for a conversation that does not exist.
        """
        if user_id is None:
            await self.get(conversation_id, tenant_id)
        else:
            await self._owned(conversation_id, tenant_id, user_id)
        return await self.store.messages(conversation_id, tenant_id, limit, before)

    async def feedback(
        self, conversation_id: str, message_id: str, tenant_id: str, user_id: str
    ) -> ConversationFeedback | None:
        conversation = await self.get(conversation_id, tenant_id)
        if conversation.user_id != user_id:
            raise NotFoundError(f"conversation {conversation_id!r} not found")
        return await self.feedback_store.get(tenant_id, user_id, message_id)

    async def record_feedback(
        self,
        conversation_id: str,
        message_id: str,
        tenant_id: str,
        user_id: str,
        rating: FeedbackRating,
        *,
        category: str | None = None,
        comment: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> ConversationFeedback:
        conversation = await self.get(conversation_id, tenant_id)
        if conversation.user_id != user_id:
            raise NotFoundError(f"conversation {conversation_id!r} not found")
        messages = await self.store.messages(conversation_id, tenant_id, limit=1000)
        message = next((item for item in messages if item.id == message_id), None)
        if message is None:
            raise NotFoundError(f"assistant message {message_id!r} not found")
        saved = await record_feedback(
            store=self.feedback_store,
            conversation=conversation,
            message=message,
            user_id=user_id,
            rating=rating,
            category=category,
            comment=comment,
            tags=tags,
            audits=self._audits,
        )
        await self._emit(
            conversation,
            "conversation.feedback.recorded",
            {
                "feedback_id": saved.id,
                "message_id": saved.message_id,
                "rating": saved.rating.value,
            },
            saved.message_id,
        )
        return saved

    async def submit(
        self,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
        *,
        credentials: Mapping[str, str] | None = None,
        attachments: tuple[RunAttachment, ...] = (),
        run_input: str | None = None,
        agent: str | None = None,
    ) -> tuple[ConversationMessage, ConversationMessage]:
        """Accept a user message and answer it in the background.

        ``agent`` answers this turn instead of the conversation's agent; it must be the
        conversation's agent or one of its ``allowed_agents`` (``ValueError`` otherwise). The
        assistant message records it as ``agent``.

        ``run_input`` is the text the turn's run gets instead of ``text``, for a host that shapes
        it (today's date, references to attachments, context of its own). The stored message,
        its events and the history keep ``text``; ``run_input`` is not stored. The handler must
        accept a ``run_input`` keyword, as :class:`RuntimeConversationHandler` does. Without it,
        the run gets ``text`` as before.

        ``attachments`` (uploaded files, by artifact reference) are kept on the message as file
        blocks and handed to the turn's run; the agent must accept them.

        ``credentials`` are the caller's, for the turn's tools (see ``Runtime.start``). They are
        kept in memory only, for this turn, and never stored, emitted or logged. A turn that pauses
        drops them; pass them again to :meth:`respond`.
        """
        credentials = read_only_credentials(credentials)
        if not self._accepting:
            raise ConflictError("conversation service is draining")
        if not text.strip():
            raise ValueError("message cannot be empty")
        conversation = await self._owned(conversation_id, tenant_id, user_id)
        if agent is not None and not conversation.allows(agent):
            raise ValueError(f"agent {agent!r} is not allowed in this conversation")
        turn_agent = None if agent is None or agent == conversation.agent else agent
        handler = self.handlers.get(conversation.handler)
        if credentials and not _accepts(handler.handle, "credentials"):
            raise ConfigurationError(
                f"conversation handler {handler.name!r} does not accept credentials"
            )
        if run_input is not None:
            if not run_input.strip():
                raise ValueError("run_input cannot be empty")
            if not _accepts(handler.handle, "run_input"):
                raise ConfigurationError(
                    f"conversation handler {handler.name!r} does not accept run_input"
                )
        async with self._locks.hold(conversation.id):
            active = self._tasks.get(conversation_id)
            if active and not active.done():
                raise ConflictError("conversation already has an active response")
            awaiting = await self._awaiting_message(conversation)
            if awaiting is not None:
                await self._reject_while_pending(conversation, awaiting)
            user_message = ConversationMessage(
                conversation_id=conversation.id,
                tenant_id=tenant_id,
                role=Role.USER,
                content=(
                    TextBlock(text=text.strip()),
                    *(
                        FileBlock(artifact_id=item.artifact_id, media_type=item.media_type)
                        for item in attachments
                    ),
                ),
                metadata=redact(metadata or {}),
            )
            await self.store.append_message(user_message)
            assistant_message = ConversationMessage(
                conversation_id=conversation.id,
                tenant_id=tenant_id,
                role=Role.ASSISTANT,
                content=(),
                status=MessageStatus.ACCEPTED,
                reply_to_message_id=user_message.id,
                agent=turn_agent,
            )
            await self.store.append_message(assistant_message)
            if conversation.title == "New conversation":
                expected = conversation.version
                conversation.title = text.strip()[:80]
                conversation.updated_at = utc_now()
                await self.store.save(conversation, expected)
            await self._emit(
                conversation,
                "conversation.message.accepted",
                {"role": "user", "text": text.strip()},
                user_message.id,
            )
            turn = _for_turn(conversation, assistant_message)

            async def work(
                emit: Callable[[str, dict[str, Any] | None], Awaitable[None]],
                history: Sequence[ConversationMessage],
            ) -> ConversationTurnResult:
                options: dict[str, Any] = {}
                if credentials:
                    options["credentials"] = credentials
                if run_input is not None:
                    options["run_input"] = run_input
                if options:
                    handle: Any = handler.handle
                    result: ConversationTurnResult = await handle(
                        turn, user_message, history, emit, **options
                    )
                    return result
                return await handler.handle(turn, user_message, history, emit)

            task = self._start(self._respond(turn, user_message, assistant_message, work))
            self._tasks[conversation.id] = task
            task.add_done_callback(self._forget_task)
            return user_message, assistant_message

    async def respond(
        self,
        conversation_id: str,
        turn_id: str,
        tenant_id: str,
        user_id: str,
        response: ConversationTurnResponse,
        *,
        credentials: Mapping[str, str] | None = None,
    ) -> ConversationMessage:
        """Answer a turn that awaits approval or input, and continue it in the background.

        ``turn_id`` is the id of the user message that started the turn (``pending.turn_id``).
        ``response`` is a :class:`ConversationApprovalDecision` for ``awaiting_approval`` or a
        :class:`ConversationInputAnswer` for ``awaiting_input`` or a
        :class:`ConversationCredentialsRenewal` with ``credentials`` for ``awaiting_credentials``
        (a run recovered after a restart without them). The turn keeps streaming into the
        same assistant message, which is returned in ``streaming`` status. ``credentials`` are as
        for :meth:`submit`: the resumed turn sees only these.

        Raises ``NotFoundError`` for an unknown turn and ``ConflictError`` when the turn is not
        waiting (already answered, cancelled or expired) or the conversation is busy.
        """
        credentials = read_only_credentials(credentials)
        if not self._accepting:
            raise ConflictError("conversation service is draining")
        conversation = await self._owned(conversation_id, tenant_id, user_id)
        handler: Any = self.handlers.get(conversation.handler)
        if getattr(handler, "resume", None) is None or getattr(handler, "follow", None) is None:
            raise ConflictError(f"conversation handler {handler.name!r} cannot resume turns")
        async with self._locks.hold(conversation.id):
            active = self._tasks.get(conversation_id)
            if active and not active.done():
                raise ConflictError("conversation already has an active response")
            user_message, assistant = await self._turn(conversation, turn_id)
            pending = assistant.pending
            if assistant.status not in AWAITING_MESSAGE_STATUSES or pending is None:
                raise ConflictError(
                    f"turn {turn_id!r} is not waiting for a response "
                    f"(status {assistant.status.value!r})"
                )
            try:
                await handler.resume(conversation, pending, response, credentials=credentials)
            except PendingTurnExpiredError:
                await self._store_result(
                    conversation,
                    assistant,
                    ConversationTurnResult(
                        content=(TextBlock(text=_EXPIRED_TEXT),),
                        run_ids=assistant.run_ids,
                        outcome="expired",
                        metadata={"reason": "approval_expired"},
                    ),
                )
                raise
            except ConflictError:
                if await self._pending_state(conversation, assistant) == "ended":
                    await self._settle(conversation, assistant)
                    raise ConflictError(f"turn {turn_id!r} is no longer waiting") from None
                raise
            streaming = assistant.model_copy(
                update={"status": MessageStatus.STREAMING, "pending": None, "updated_at": utc_now()}
            )
            await self.store.update_message(streaming)
            resumed: dict[str, Any] = {
                "turn_id": turn_id,
                "kind": pending.kind,
                "decided_by": user_id,
            }
            if isinstance(response, ConversationApprovalDecision):
                resumed.update(approval_id=pending.approval_id, decision=response.decision)
            elif isinstance(response, ConversationInputAnswer):
                resumed["text"] = response.text
            await self._emit(conversation, "assistant.message.resumed", resumed, assistant.id)
            self._follow(_for_turn(conversation, assistant), user_message, streaming, pending)
            return streaming

    async def steer(
        self, conversation_id: str, turn_id: str, tenant_id: str, user_id: str, text: str
    ) -> SteeringMessage:
        """Send the user more instructions for a turn that is still working.

        The text reaches the turn's run (``Runtime.steer``): the model reads it at the run's
        next step, after the current tool results. ``agent.steer.delivered`` follows on the
        conversation, or ``agent.steer.undelivered`` if the run ends first; the turn's run must
        be followed by this process for those (it is, for a turn submitted or recovered here).
        An ``assistant.message.steered`` event records the request, with sizes, not text.

        Only the conversation's owner may steer it (``NotFoundError`` otherwise). Raises
        ``ConflictError`` when the turn has no run yet, has ended, or its run cannot take more
        steering, and ``ValueError`` for empty or too long text.
        """
        conversation = await self._owned(conversation_id, tenant_id, user_id)
        handler: Any = self.handlers.get(conversation.handler)
        steer = getattr(handler, "steer", None)
        if steer is None:
            raise ConflictError(f"conversation handler {handler.name!r} cannot steer turns")
        _, assistant = await self._turn(conversation, turn_id)
        if (
            assistant.status not in _IN_PROGRESS | AWAITING_MESSAGE_STATUSES
            or not assistant.run_ids
        ):
            raise ConflictError(f"turn {turn_id!r} is not running")
        message: SteeringMessage = await steer(
            _for_turn(conversation, assistant), assistant.run_ids[-1], text
        )
        await self._emit(
            conversation,
            "assistant.message.steered",
            {"turn_id": turn_id, "steer_id": message.id, "chars": len(message.text)},
            assistant.id,
        )
        return message

    async def cancel(self, conversation_id: str, tenant_id: str) -> None:
        """Cancel the active response, or end a turn that awaits approval or input."""
        conversation = await self.get(conversation_id, tenant_id)
        task = self._tasks.get(conversation_id)
        if task and not task.done():
            task.cancel()
            return
        async with self._locks.hold(conversation.id):
            awaiting = await self._awaiting_message(conversation)
            if awaiting is None:
                return
            await self._end_pending_run(conversation, awaiting)
            cancelled = awaiting.model_copy(
                update={
                    "status": MessageStatus.CANCELLED,
                    "content": (TextBlock(text="Response cancelled."),),
                    "pending": None,
                    "updated_at": utc_now(),
                }
            )
            await self.store.update_message(cancelled)
            await self._emit(
                conversation,
                "assistant.message.cancelled",
                {"turn_id": awaiting.pending.turn_id if awaiting.pending else None},
                awaiting.id,
            )

    async def recover(self, limit: int = 1000) -> int:
        """Settle or follow the turns a stopped process left streaming; return how many.

        Run it after ``Runtime.recover()`` (``Container.astart`` does), so that the runs it
        resumed are driven by this process and their turns are followed to the end. For each
        streaming turn, the run is the one stored on the message, or the turn's run in the run
        store (a stop between starting the run and storing its id):

        - a run that still works is followed to its end, into the same message;
        - a run that ended or paused meanwhile settles the turn at once: completed with the
          answer, awaiting approval or input with its pending action (expired if its approval
          expired), awaiting credentials when ``Runtime.recover`` paused the run for the
          caller's credentials it was started with (continue it with ``respond`` and a
          :class:`ConversationCredentialsRenewal`), failed or cancelled with the run's own outcome;
        - a turn with no run, or whose run is gone, fails as interrupted (retryable).

        The turn's work is never started again. Settling uses a conditional update, so calling it
        again, or from two processes at once, settles each turn once. Turns that await approval
        or input stay pending; see :meth:`reconcile_pending_turns`.
        """
        recovered = 0
        for assistant in await self.store.pending_messages(limit):
            conversation = await self.store.get(assistant.conversation_id, assistant.tenant_id)
            if conversation is None:
                continue
            try:
                async with self._locks.hold(conversation.id):
                    active = self._tasks.get(conversation.id)
                    if active and not active.done():
                        continue  # this process is answering it
                    if await self._reattach(conversation, assistant, fail_without_run=True):
                        recovered += 1
            except Exception as exc:
                structlog.get_logger("algen_agent_runtime.conversations").warning(
                    "conversation_turn_recovery_failed",
                    conversation_id=assistant.conversation_id,
                    message_id=assistant.id,
                    error_type=type(exc).__name__,
                )
        return recovered + await self.reconcile_pending_turns(limit)

    async def follow(
        self, conversation_id: str, turn_id: str, tenant_id: str, user_id: str
    ) -> ConversationMessage:
        """Re-attach to a turn that is still streaming, and return its assistant message.

        For a host whose process restarted, or that takes a turn over from another process: the
        turn's run is followed in the background into the same message, whose events reach the
        conversation's subscribers as for any turn. A run that already ended or paused settles
        the turn at once. It never starts the turn's work again.

        Idempotent: a turn this process already answers, or that no longer streams, is returned
        as it is. A turn with no run yet is returned as it is too (its process may still be
        starting it); :meth:`recover` fails such turns after a restart. Raises ``NotFoundError``
        for an unknown turn or another user's conversation.
        """
        if not self._accepting:
            raise ConflictError("conversation service is draining")
        conversation = await self._owned(conversation_id, tenant_id, user_id)
        async with self._locks.hold(conversation.id):
            _, assistant = await self._turn(conversation, turn_id)
            active = self._tasks.get(conversation_id)
            if assistant.status not in _IN_PROGRESS or (active and not active.done()):
                return assistant
            await self._reattach(conversation, assistant, fail_without_run=False)
            _, current = await self._turn(conversation, turn_id)
            return current

    async def reconcile_pending_turns(self, limit: int = 1000) -> int:
        """End or reattach pending turns whose run no longer waits; return how many changed.

        A turn still paused is left as it is. One whose run ended while it waited (expired,
        cancelled or failed) ends with that status. One whose run was resumed but whose message
        was not updated (a restart between the two) is followed again, never re-run. Safe to call
        from a maintenance loop and after a restart.
        """
        lister = getattr(self.store, "awaiting_messages", None)
        if lister is None:
            return 0
        changed = 0
        for message in await lister(limit):
            conversation = await self.store.get(message.conversation_id, message.tenant_id)
            if conversation is None or message.pending is None:
                continue
            try:
                async with self._locks.hold(conversation.id):
                    active = self._tasks.get(conversation.id)
                    if active and not active.done():
                        continue
                    user_message, current = await self._turn(conversation, message.pending.turn_id)
                    pending = current.pending
                    if current.status not in AWAITING_MESSAGE_STATUSES or pending is None:
                        continue
                    state = await self._pending_state(conversation, current)
                    if state == "paused":
                        continue
                    if state == "ended":
                        await self._settle(conversation, current)
                    else:
                        streaming = current.model_copy(
                            update={
                                "status": MessageStatus.STREAMING,
                                "pending": None,
                                "updated_at": utc_now(),
                            }
                        )
                        await self.store.update_message(streaming)
                        self._follow(
                            _for_turn(conversation, current),
                            user_message,
                            streaming,
                            pending,
                            recovered=True,
                        )
                    changed += 1
            except Exception as exc:
                structlog.get_logger("algen_agent_runtime.conversations").warning(
                    "conversation_pending_turn_reconcile_failed",
                    conversation_id=message.conversation_id,
                    message_id=message.id,
                    error_type=type(exc).__name__,
                )
        return changed

    async def shutdown(self, grace_seconds: float = 10) -> None:
        """Stop accepting turns, let active ones finish for ``grace_seconds``, then detach.

        A turn still answering after the grace period is detached from its run, not cancelled:
        the run keeps going for ``Runtime.shutdown`` to finish or save for recovery, and the
        message stays ``streaming`` with its run id, for the next start's :meth:`recover` to
        follow or settle. :meth:`cancel` still cancels a turn and its run.
        """
        self._accepting = False
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        self._detach.set()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _owned(self, conversation_id: str, tenant_id: str, user_id: str) -> Conversation:
        conversation = await self.get(conversation_id, tenant_id)
        if conversation.user_id != user_id:
            raise NotFoundError(f"conversation {conversation_id!r} not found")
        return conversation

    async def _turn(
        self, conversation: Conversation, turn_id: str
    ) -> tuple[ConversationMessage, ConversationMessage]:
        """The user message ``turn_id`` and the assistant message that answers it."""
        messages = await self.store.messages(conversation.id, conversation.tenant_id, limit=1000)
        user_message = next(
            (item for item in messages if item.id == turn_id and item.role == Role.USER), None
        )
        assistant = next(
            (
                item
                for item in reversed(messages)
                if item.role == Role.ASSISTANT and item.reply_to_message_id == turn_id
            ),
            None,
        )
        if user_message is None or assistant is None:
            raise NotFoundError(f"turn {turn_id!r} not found")
        return user_message, assistant

    async def _awaiting_message(self, conversation: Conversation) -> ConversationMessage | None:
        """The assistant message of the turn that awaits approval or input, if any."""
        messages = await self.store.messages(conversation.id, conversation.tenant_id, limit=50)
        return next(
            (
                item
                for item in reversed(messages)
                if item.role == Role.ASSISTANT and item.status in AWAITING_MESSAGE_STATUSES
            ),
            None,
        )

    async def _reject_while_pending(
        self, conversation: Conversation, awaiting: ConversationMessage
    ) -> None:
        """Refuse a new message while a turn waits; settle a turn whose run already ended."""
        state = await self._pending_state(conversation, awaiting)
        if state == "ended":
            await self._settle(conversation, awaiting)
            return
        if state == "running":
            # Resumed just before a restart; recovery is following it.
            raise ConflictError("conversation already has an active response")
        turn_id = awaiting.pending.turn_id if awaiting.pending else awaiting.reply_to_message_id
        raise ConflictError(
            f"conversation has a turn waiting for {_awaited(awaiting.status)}: turn {turn_id!r}; "
            "answer or cancel it first"
        )

    async def _pending_state(
        self, conversation: Conversation, message: ConversationMessage
    ) -> Literal["paused", "running", "ended"]:
        handler: Any = self.handlers.get(conversation.handler)
        pending_state = getattr(handler, "pending_state", None)
        if pending_state is None or message.pending is None:
            return "paused"
        state: Literal["paused", "running", "ended"] = await pending_state(
            conversation, message.pending
        )
        return state

    async def _settle(self, conversation: Conversation, message: ConversationMessage) -> None:
        """End a pending turn whose run ended while it waited, with the run's outcome."""
        handler: Any = self.handlers.get(conversation.handler)
        settle = getattr(handler, "settle", None)
        if settle is None or message.pending is None:
            result = ConversationTurnResult(
                content=(TextBlock(text=_EXPIRED_TEXT),), run_ids=message.run_ids, outcome="expired"
            )
        else:
            try:
                result = await settle(conversation, message.pending)
            except Exception as exc:
                result = ConversationTurnResult(
                    content=(TextBlock(text=self.presentation.unexpected_error_message(exc)),),
                    run_ids=message.run_ids,
                    outcome="failed",
                    metadata={"error_type": type(exc).__name__},
                )
        await self._store_result(conversation, message, result)

    async def _end_pending_run(
        self, conversation: Conversation, awaiting: ConversationMessage | None = None
    ) -> None:
        """Best effort: end the paused run behind the conversation's pending turn."""
        try:
            awaiting = awaiting or await self._awaiting_message(conversation)
            if awaiting is None or awaiting.pending is None:
                return
            handler: Any = self.handlers.get(conversation.handler)
            cancel_pending = getattr(handler, "cancel_pending", None)
            if cancel_pending is not None:
                await cancel_pending(conversation, awaiting.pending)
        except (NotFoundError, ConflictError):
            return

    def _follow(
        self,
        conversation: Conversation,
        user_message: ConversationMessage,
        streaming: ConversationMessage,
        pending: ConversationPendingAction,
        *,
        recovered: bool = False,
    ) -> None:
        handler: Any = self.handlers.get(conversation.handler)

        async def work(
            emit: Callable[[str, dict[str, Any] | None], Awaitable[None]],
            history: Sequence[ConversationMessage],
        ) -> ConversationTurnResult:
            del history
            result: ConversationTurnResult = await handler.follow(conversation, pending, emit)
            return result

        self._spawn(conversation, user_message, streaming, work, recovered=recovered)

    def _start(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Run a turn in a task that sees this service's detach signal (see ``shutdown``)."""
        context = contextvars.copy_context()
        context.run(DETACH_SIGNAL.set, self._detach)
        return asyncio.create_task(work, context=context)

    def _spawn(
        self,
        conversation: Conversation,
        user_message: ConversationMessage,
        streaming: ConversationMessage,
        work: _TurnWork,
        *,
        recovered: bool,
    ) -> None:
        task = self._start(
            self._respond(
                conversation, user_message, streaming, work, resumed=True, recovered=recovered
            )
        )
        self._tasks[conversation.id] = task
        task.add_done_callback(self._forget_task)

    async def _reattach(
        self,
        conversation: Conversation,
        assistant: ConversationMessage,
        *,
        fail_without_run: bool,
    ) -> bool:
        """Follow (or settle) the run behind a streaming turn; fail it when it has none.

        Called under the conversation's lock with no local task answering the conversation. Returns
        whether the turn was taken over or settled here.
        """
        conversation = _for_turn(conversation, assistant)
        handler: Any = self.handlers.get(conversation.handler)
        follow_turn = getattr(handler, "follow_turn", None)
        turn_id = assistant.reply_to_message_id
        run_id = assistant.run_ids[-1] if assistant.run_ids else None
        turn_run = getattr(handler, "turn_run", None)
        if run_id is None and turn_id and turn_run is not None:
            run_id = await turn_run(conversation, turn_id)
        user_message: ConversationMessage | None = None
        if turn_id:
            messages = await self.store.messages(
                conversation.id, conversation.tenant_id, limit=1000
            )
            user_message = next(
                (item for item in messages if item.id == turn_id and item.role == Role.USER), None
            )
        if follow_turn is None or run_id is None or user_message is None or turn_id is None:
            if not fail_without_run:
                return False
            return await self._interrupt(conversation, assistant)
        streaming = assistant.model_copy(
            update={
                "status": MessageStatus.STREAMING,
                "run_ids": tuple(dict.fromkeys((*assistant.run_ids, run_id))),
                "updated_at": utc_now(),
            }
        )
        if not await self._write(streaming, _IN_PROGRESS):
            return False  # another process settled it first
        followed_run: str = run_id
        followed_turn: str = turn_id

        async def work(
            emit: Callable[[str, dict[str, Any] | None], Awaitable[None]],
            history: Sequence[ConversationMessage],
        ) -> ConversationTurnResult:
            del history
            result: ConversationTurnResult = await follow_turn(
                conversation, followed_turn, followed_run, emit
            )
            return result

        self._spawn(conversation, user_message, streaming, work, recovered=True)
        return True

    async def _interrupt(self, conversation: Conversation, assistant: ConversationMessage) -> bool:
        """Fail a turn whose work is gone: interrupted, retryable. False if already settled."""
        failed = assistant.model_copy(
            update={
                "status": MessageStatus.FAILED,
                "content": (TextBlock(text=_INTERRUPTED_TEXT),),
                "metadata": {"error_type": "InterruptedResponse", "retryable": True},
                "updated_at": utc_now(),
            }
        )
        if not await self._write(failed, _IN_PROGRESS):
            return False
        await self._emit(
            conversation,
            "assistant.message.failed",
            {"error_type": "InterruptedResponse", "retryable": True, "message": _INTERRUPTED_TEXT},
            assistant.id,
        )
        return True

    async def _write(
        self, message: ConversationMessage, expected: Collection[MessageStatus] | None
    ) -> bool:
        """Store ``message``; with ``expected``, only while the stored one has such a status."""
        if expected is None:
            await self.store.update_message(message)
            return True
        conditional = getattr(self.store, "update_message_if", None)
        if conditional is not None:
            return bool(await conditional(message, expected))
        messages = await self.store.messages(message.conversation_id, message.tenant_id, limit=1000)
        current = next((item for item in messages if item.id == message.id), None)
        if current is None or current.status not in expected:
            return False
        await self.store.update_message(message)
        return True

    async def _respond(
        self,
        conversation: Conversation,
        user_message: ConversationMessage,
        assistant_message: ConversationMessage,
        work: _TurnWork,
        *,
        resumed: bool = False,
        recovered: bool = False,
    ) -> None:
        attributes: dict[str, Any] = {
            "span.type": "agent",
            "agent.span.type": "agent",
            "gen_ai.operation.name": "invoke_agent",
            "agent.id": conversation.agent,
            "agent.name": conversation.agent,
            "session.id": conversation.id,
            "gen_ai.conversation.id": conversation.id,
            "conversation.id": conversation.id,
            "conversation.handler": conversation.handler,
            "conversation.title": conversation.title,
            "interaction.id": user_message.id,
            "turn.id": user_message.id,
            "message.id": assistant_message.id,
            "user.id": conversation.user_id,
            "tenant.id": conversation.tenant_id,
        }
        if resumed:
            attributes["conversation.turn.resumed"] = True
        if self._telemetry_include_content and not resumed:
            attributes["conversation.input"] = self._telemetry_content(user_message.text_content)
        with self._observability.run_scope(
            agent_id=conversation.agent,
            agent_name=conversation.agent,
            tenant_id=conversation.tenant_id,
        ):
            span_scope = self._observability.span_scope(
                "conversation.turn", attributes=attributes, root=True
            ) or self._tracer.start_as_current_span(
                "conversation.turn", context=Context(), attributes=attributes
            )
            with span_scope as span:
                context_getter = getattr(span, "get_span_context", None)
                span_context = context_getter() if context_getter is not None else None
                trace_context = (
                    {
                        "trace_id": f"{span_context.trace_id:032x}",
                        "span_id": f"{span_context.span_id:016x}",
                    }
                    if span_context is not None and span_context.is_valid
                    else None
                )
                try:
                    outcome, run_ids, block_count, output_text = await self._respond_observed(
                        conversation,
                        user_message,
                        assistant_message,
                        trace_context,
                        work=work,
                        resumed=resumed,
                        recovered=recovered,
                    )
                except asyncio.CancelledError:
                    span.set_attribute("conversation.turn.status", "cancelled")
                    span.set_attribute("agent.run.outcome", "cancelled")
                    self._observability.set_span_outcome(
                        span, failed=True, description="conversation turn cancelled"
                    )
                    raise
                span.set_attribute("conversation.turn.status", outcome)
                span.set_attribute("agent.run.outcome", outcome)
                span.set_attribute("conversation.agent_run_count", len(run_ids))
                span.set_attribute("conversation.output_block_count", block_count)
                if run_ids:
                    span.set_attribute("conversation.agent_run_ids", run_ids)
                if self._telemetry_include_content and output_text:
                    span.set_attribute("conversation.output", self._telemetry_content(output_text))
                self._observability.set_span_outcome(
                    span,
                    failed=outcome == "failed",
                    description="conversation turn failed" if outcome == "failed" else None,
                )

    async def _respond_observed(
        self,
        conversation: Conversation,
        user_message: ConversationMessage,
        assistant_message: ConversationMessage,
        trace_context: dict[str, str] | None = None,
        *,
        work: _TurnWork | None = None,
        resumed: bool = False,
        recovered: bool = False,
    ) -> tuple[str, tuple[str, ...], int, str]:
        # A recovered turn may be followed by two processes: only the first ends it.
        expected = frozenset({MessageStatus.STREAMING}) if recovered else None
        if resumed:
            streaming = assistant_message
        else:
            streaming = assistant_message.model_copy(
                update={"status": MessageStatus.STREAMING, "updated_at": utc_now()}
            )
            await self.store.update_message(streaming)
            await self._emit(
                conversation,
                "assistant.message.started",
                {},
                assistant_message.id,
            )

        async def emit(event_type: str, data: dict[str, Any] | None = None) -> None:
            nonlocal streaming
            payload = dict(data or {})
            if event_type == DELTA_EVENT:
                payload = {"message_id": assistant_message.id, **payload}
            elif event_type == RUN_STARTED_EVENT and payload.get("run_id"):
                # Stored at once, so a restarted service follows this run instead of failing
                # the turn.
                run_id = str(payload["run_id"])
                if run_id not in streaming.run_ids:
                    streaming = streaming.model_copy(
                        update={"run_ids": (*streaming.run_ids, run_id), "updated_at": utc_now()}
                    )
                    await self.store.update_message(streaming)
            try:
                await self._emit(conversation, event_type, payload, assistant_message.id)
            except Exception as exc:
                # A lost progress event must not abandon a turn whose run goes on.
                structlog.get_logger("algen_agent_runtime.conversations").warning(
                    "conversation_event_publish_failed",
                    conversation_id=conversation.id,
                    event_type=event_type,
                    error_type=type(exc).__name__,
                )

        if work is None:
            handler = self.handlers.get(conversation.handler)

            async def default_work(
                emit: Callable[[str, dict[str, Any] | None], Awaitable[None]],
                history: Sequence[ConversationMessage],
            ) -> ConversationTurnResult:
                return await handler.handle(conversation, user_message, history, emit)

            work = default_work

        try:
            history = await self.store.messages(conversation.id, conversation.tenant_id, limit=1000)
            result = await work(emit, history)
            suggested_followups = result.suggested_followups
            if (
                not suggested_followups
                and result.outcome == "completed"
                and self._followup_provider is not None
            ):
                output_text = _output_text(result)
                try:
                    suggested_followups = await self._followup_provider.suggest(
                        tenant_id=conversation.tenant_id,
                        user_id=conversation.user_id,
                        conversation_id=conversation.id,
                        history=history,
                        user_text=user_message.text_content,
                        assistant_text=output_text,
                    )
                except Exception as exc:
                    structlog.get_logger("algen_agent_runtime.conversations").warning(
                        "conversation_followup_generation_failed",
                        conversation_id=conversation.id,
                        error_type=type(exc).__name__,
                    )
            output_text = await self._store_result(
                conversation,
                streaming,
                result,
                suggested_followups=suggested_followups,
                trace_context=trace_context,
                expected=expected,
            )
            return result.outcome, result.run_ids, len(result.content), output_text
        except asyncio.CancelledError:
            if detaching():
                # Shutting down: the run goes on or is saved for recovery, and the message stays
                # streaming with its run id for the next start's recover().
                raise
            cancelled = streaming.model_copy(
                update={
                    "status": MessageStatus.CANCELLED,
                    "content": (TextBlock(text="Response cancelled."),),
                    "updated_at": utc_now(),
                }
            )
            if await self._write(cancelled, expected):
                await emit("assistant.message.cancelled", {})
            raise
        except ConversationTurnInterruptedError:
            await self._interrupt(conversation, streaming)
            return "failed", streaming.run_ids, 1, _INTERRUPTED_TEXT
        except Exception as exc:
            public_message = self.presentation.unexpected_error_message(exc)
            structlog.get_logger("algen_agent_runtime.conversations").exception(
                "conversation_response_failed",
                conversation_id=conversation.id,
                message_id=assistant_message.id,
                tenant_id=conversation.tenant_id,
                error_type=type(exc).__name__,
            )
            failed = streaming.model_copy(
                update={
                    "status": MessageStatus.FAILED,
                    "content": (TextBlock(text=public_message),),
                    "metadata": {
                        "error_type": type(exc).__name__,
                        "retryable": bool(getattr(exc, "retryable", False)),
                    },
                    "updated_at": utc_now(),
                }
            )
            if not await self._write(failed, expected):
                return "failed", (), 1, public_message
            await emit(
                "assistant.message.failed",
                {
                    "error_type": type(exc).__name__,
                    "message": public_message,
                    "retryable": bool(getattr(exc, "retryable", False)),
                },
            )
            return "failed", (), 1, public_message

    async def _store_result(
        self,
        conversation: Conversation,
        message: ConversationMessage,
        result: ConversationTurnResult,
        *,
        suggested_followups: tuple[str, ...] | None = None,
        trace_context: dict[str, str] | None = None,
        expected: Collection[MessageStatus] | None = None,
    ) -> str:
        """Record the turn's result on its assistant message and announce it; return its text.

        With ``expected``, only while the stored message has such a status: a turn another
        process already ended is left as it is and not announced again.
        """
        status = _STATUS_BY_OUTCOME.get(result.outcome, MessageStatus.COMPLETED)
        updated = message.model_copy(
            update={
                "content": result.content,
                "status": status,
                "run_ids": tuple(dict.fromkeys((*message.run_ids, *result.run_ids))),
                "artifacts": result.artifacts,
                "citations": result.citations,
                "suggested_followups": (
                    result.suggested_followups
                    if suggested_followups is None
                    else suggested_followups
                ),
                "pending": result.pending,
                "metadata": {
                    **redact(result.metadata),
                    **({"runtime_trace_context": trace_context} if trace_context else {}),
                },
                "updated_at": utc_now(),
            }
        )
        output_text = _output_text(result)
        if not await self._write(updated, expected):
            return output_text
        event_payload = updated.model_dump(mode="json")
        if status == MessageStatus.FAILED:
            event_payload["message"] = output_text
        if result.pending is not None:
            event_payload["turn_id"] = result.pending.turn_id
        try:
            await self._emit(conversation, _EVENT_BY_STATUS[status], event_payload, message.id)
        except Exception as exc:
            # The message is stored: a reader gets it from messages(), so the turn stands.
            structlog.get_logger("algen_agent_runtime.conversations").warning(
                "conversation_event_publish_failed",
                conversation_id=conversation.id,
                event_type=_EVENT_BY_STATUS[status],
                error_type=type(exc).__name__,
            )
        return output_text

    def _telemetry_content(self, value: str) -> str:
        sanitized = str(redact(value))
        if len(sanitized) <= self._telemetry_max_content_chars:
            return sanitized
        marker = "...[TRUNCATED]"
        return sanitized[: self._telemetry_max_content_chars - len(marker)] + marker

    def _forget_task(self, completed: asyncio.Task[None]) -> None:
        if not completed.cancelled() and completed.exception() is not None:
            exception = completed.exception()
            structlog.get_logger("algen_agent_runtime.conversations").error(
                "conversation_task_terminated_unexpectedly",
                error_type=type(exception).__name__,
                error=str(exception)[:500],
            )
        for conversation_id, task in tuple(self._tasks.items()):
            if task is completed:
                self._tasks.pop(conversation_id, None)
                return

    async def _emit(
        self,
        conversation: Conversation,
        event_type: str,
        data: dict[str, Any],
        message_id: str | None = None,
    ) -> None:
        # Sequences are allocated one at a time per conversation in this process; a sequence
        # another process took first (a unique violation, as ConflictError) is allocated again.
        async with self._emit_locks.hold(conversation.id):
            for attempt in range(_PUBLISH_ATTEMPTS):
                sequence = await self.events.next_sequence(conversation.id)
                try:
                    await self.events.publish(
                        ConversationEvent(
                            type=event_type,
                            conversation_id=conversation.id,
                            tenant_id=conversation.tenant_id,
                            message_id=message_id,
                            sequence=sequence,
                            data=data,
                        )
                    )
                    break
                except ConflictError:
                    if attempt == _PUBLISH_ATTEMPTS - 1:
                        raise
                    await asyncio.sleep(0.005 * (attempt + 1))
        # Text deltas are transient presentation; the completed message is the audited record.
        if self._audits is not None and event_type != DELTA_EVENT:
            await self._audits.append(
                AuditEvent(
                    action=event_type,
                    outcome="failed" if event_type.endswith(".failed") else "ok",
                    tenant_id=conversation.tenant_id,
                    actor_id=conversation.user_id,
                    resource_id=conversation.id,
                    metadata={"message_id": message_id} if message_id else {},
                )
            )

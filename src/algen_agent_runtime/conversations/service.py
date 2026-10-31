from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

import structlog
from opentelemetry.context import Context

from algen_agent_runtime.approvals.service import ApprovalStatus
from algen_agent_runtime.conversations.contracts import (
    AWAITING_MESSAGE_STATUSES,
    Conversation,
    ConversationApprovalDecision,
    ConversationEvent,
    ConversationEventPublisher,
    ConversationHandler,
    ConversationInputAnswer,
    ConversationMessage,
    ConversationPendingAction,
    ConversationStatus,
    ConversationStore,
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
from algen_agent_runtime.conversations.streaming import DELTA_EVENT, DeltaCoalescer, follow_run
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
    TextBlock,
    utc_now,
)
from algen_agent_runtime.types.interfaces import Runtime


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
    ) -> None:
        self._runtime = runtime
        self._presentation = presentation or ConversationPresentation()
        self._stream = stream
        self._delta_interval = delta_interval_seconds
        self._delta_max_chars = delta_max_chars
        # The run's last event sequence just before each resume, so ``follow`` forwards every
        # delta of the resumed drive even if it starts before ``follow`` subscribes.
        self._resume_cursors: dict[str, int] = {}

    async def handle(
        self,
        conversation: Conversation,
        user_message: ConversationMessage,
        history: Sequence[ConversationMessage],
        emit: Any,
        *,
        credentials: Mapping[str, str] | None = None,
    ) -> ConversationTurnResult:
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
                input=user_message.text_content,
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
        result = await follow_run(
            self._runtime,
            state.id,
            conversation.tenant_id,
            self._coalescer(emit) if stream else None,
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
            approvals = getattr(self._runtime, "approvals", None)
            if approvals is not None and pending.approval_id:
                approval = await approvals.get(pending.approval_id, conversation.tenant_id)
                if approval is not None and (
                    approval.status == ApprovalStatus.EXPIRED
                    or (
                        approval.status == ApprovalStatus.PENDING
                        and approval.expires_at <= utc_now()
                    )
                ):
                    raise PendingTurnExpiredError(f"turn {pending.turn_id!r} approval expired")
            payload = {"decision": response.decision}
            if response.modified_parameters is not None:
                payload["modified_parameters"] = response.modified_parameters
            if response.reason:
                payload["reason"] = response.reason
        else:
            if not isinstance(response, ConversationInputAnswer):
                raise ConflictError(f"turn {pending.turn_id!r} awaits an answer, not a decision")
            payload = {"clarification": response.text}
        cursor = await self._event_cursor(run_id)
        await self._runtime.resume(run_id, conversation.tenant_id, payload, credentials=credentials)
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
        )
        return await self._turn_result(conversation, pending.turn_id, result, paused=False)

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
        if result.error:
            raise RuntimeError(result.error)
        return ConversationTurnResult(
            content=(TextBlock(text=result.output or ""),),
            run_ids=run_ids,
            artifacts=result.artifacts,
            citations=tuple(item.model_dump(mode="json") for item in result.citations),
        )

    async def _pending(
        self, conversation: Conversation, turn_id: str, run_id: str
    ) -> ConversationPendingAction:
        state = await self._run_state(run_id, conversation.tenant_id)
        payload: dict[str, Any] = dict((state.pause_payload if state else None) or {})
        paused_at = _parse_time(payload.get("paused_at")) or utc_now()
        if state is not None and state.status == RunStatus.AWAITING_CLARIFICATION:
            return ConversationPendingAction(
                kind="input",
                turn_id=turn_id,
                run_id=run_id,
                question=_optional_text(payload.get("question")),
                paused_at=paused_at,
            )
        call_id = str(payload.get("tool_call_id") or "")
        calls = state.pending_tool_calls if state is not None else []
        tool_name = next((call.name for call in calls if call.id == call_id), None)
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
            parameters_schema=schema,
            paused_at=paused_at,
        )


_EXPIRY_REASONS = frozenset({"approval_expired", "clarification_expired"})
_REJECTION_REASONS = frozenset({"approval_rejected", "continuation_rejected"})
_EXPIRED_TEXT = "This request expired before it was answered. Please ask again."


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
}
_EVENT_BY_STATUS: dict[MessageStatus, str] = {
    MessageStatus.COMPLETED: "assistant.message.completed",
    MessageStatus.FAILED: "assistant.message.failed",
    MessageStatus.CANCELLED: "assistant.message.cancelled",
    MessageStatus.EXPIRED: "assistant.message.expired",
    MessageStatus.AWAITING_APPROVAL: "assistant.message.awaiting_approval",
    MessageStatus.AWAITING_INPUT: "assistant.message.awaiting_input",
}


def _output_text(result: ConversationTurnResult) -> str:
    return "\n".join(
        str(getattr(block, "text", "")) for block in result.content if getattr(block, "text", None)
    )


def _accepts_credentials(handle: Any) -> bool:
    try:
        parameters = inspect.signature(handle).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "credentials" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _awaited(status: MessageStatus) -> str:
    return "approval" if status == MessageStatus.AWAITING_APPROVAL else "input"


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
        self._lock = asyncio.Lock()
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
    ) -> Conversation:
        self.handlers.get(handler)
        conversation = Conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            agent=agent,
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
    ) -> Sequence[ConversationMessage]:
        await self.get(conversation_id, tenant_id)
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
    ) -> tuple[ConversationMessage, ConversationMessage]:
        """Accept a user message and answer it in the background.

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
        handler = self.handlers.get(conversation.handler)
        if credentials and not _accepts_credentials(handler.handle):
            raise ConfigurationError(
                f"conversation handler {handler.name!r} does not accept credentials"
            )
        async with self._lock:
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

            async def work(
                emit: Callable[[str, dict[str, Any] | None], Awaitable[None]],
                history: Sequence[ConversationMessage],
            ) -> ConversationTurnResult:
                if credentials:
                    handle: Any = handler.handle
                    result: ConversationTurnResult = await handle(
                        conversation, user_message, history, emit, credentials=credentials
                    )
                    return result
                return await handler.handle(conversation, user_message, history, emit)

            task = asyncio.create_task(
                self._respond(conversation, user_message, assistant_message, work)
            )
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
        :class:`ConversationInputAnswer` for ``awaiting_input``. The turn keeps streaming into the
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
        async with self._lock:
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
            self._follow(conversation, user_message, streaming, pending)
            return streaming

    async def cancel(self, conversation_id: str, tenant_id: str) -> None:
        """Cancel the active response, or end a turn that awaits approval or input."""
        conversation = await self.get(conversation_id, tenant_id)
        task = self._tasks.get(conversation_id)
        if task and not task.done():
            task.cancel()
            return
        async with self._lock:
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
        """Fail interrupted responses safely instead of replaying unknown side effects.

        Turns that await approval or input stay pending; see :meth:`reconcile_pending_turns`.
        """
        recovered = 0
        for assistant in await self.store.pending_messages(limit):
            conversation = await self.store.get(assistant.conversation_id, assistant.tenant_id)
            if conversation is None:
                continue
            failed = assistant.model_copy(
                update={
                    "status": MessageStatus.FAILED,
                    "content": (
                        TextBlock(
                            text="The response was interrupted during a service restart. Please retry."
                        ),
                    ),
                    "metadata": {"error_type": "InterruptedResponse"},
                    "updated_at": utc_now(),
                }
            )
            await self.store.update_message(failed)
            await self._emit(
                conversation,
                "assistant.message.failed",
                {"error_type": "InterruptedResponse", "retryable": True},
                assistant.id,
            )
            recovered += 1
        return recovered + await self.reconcile_pending_turns(limit)

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
                async with self._lock:
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
                        self._follow(conversation, user_message, streaming, pending)
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
        self._accepting = False
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
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
    ) -> None:
        handler: Any = self.handlers.get(conversation.handler)

        async def work(
            emit: Callable[[str, dict[str, Any] | None], Awaitable[None]],
            history: Sequence[ConversationMessage],
        ) -> ConversationTurnResult:
            del history
            result: ConversationTurnResult = await handler.follow(conversation, pending, emit)
            return result

        task = asyncio.create_task(
            self._respond(conversation, user_message, streaming, work, resumed=True)
        )
        self._tasks[conversation.id] = task
        task.add_done_callback(self._forget_task)

    async def _respond(
        self,
        conversation: Conversation,
        user_message: ConversationMessage,
        assistant_message: ConversationMessage,
        work: _TurnWork,
        *,
        resumed: bool = False,
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
    ) -> tuple[str, tuple[str, ...], int, str]:
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
            payload = dict(data or {})
            if event_type == DELTA_EVENT:
                payload = {"message_id": assistant_message.id, **payload}
            await self._emit(conversation, event_type, payload, assistant_message.id)

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
            )
            return result.outcome, result.run_ids, len(result.content), output_text
        except asyncio.CancelledError:
            cancelled = streaming.model_copy(
                update={
                    "status": MessageStatus.CANCELLED,
                    "content": (TextBlock(text="Response cancelled."),),
                    "updated_at": utc_now(),
                }
            )
            await self.store.update_message(cancelled)
            await emit("assistant.message.cancelled", {})
            raise
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
            await self.store.update_message(failed)
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
    ) -> str:
        """Record the turn's result on its assistant message and announce it; return its text."""
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
        await self.store.update_message(updated)
        output_text = _output_text(result)
        event_payload = updated.model_dump(mode="json")
        if status == MessageStatus.FAILED:
            event_payload["message"] = output_text
        if result.pending is not None:
            event_payload["turn_id"] = result.pending.turn_id
        await self._emit(conversation, _EVENT_BY_STATUS[status], event_payload, message.id)
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
        sequence = await self.events.next_sequence(conversation.id)
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

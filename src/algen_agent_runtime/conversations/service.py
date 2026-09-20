from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.context import Context

from algen_agent_runtime.conversations.contracts import (
    Conversation,
    ConversationEvent,
    ConversationEventPublisher,
    ConversationHandler,
    ConversationMessage,
    ConversationStatus,
    ConversationStore,
    ConversationTurnResult,
    MessageStatus,
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
from algen_agent_runtime.events.contracts import AuditEvent
from algen_agent_runtime.exceptions.errors import (
    ConflictError,
    NotFoundError,
)
from algen_agent_runtime.observability.traccia_adapter import (
    NoopObservabilityAdapter,
    ObservabilityAdapter,
)
from algen_agent_runtime.security.redaction import redact
from algen_agent_runtime.types.contracts import Role, RunRequest, TextBlock, utc_now
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
    name = "runtime"

    def __init__(
        self,
        runtime: Runtime,
        presentation: ConversationPresentation | None = None,
    ) -> None:
        self._runtime = runtime
        self._presentation = presentation or ConversationPresentation()

    async def handle(
        self,
        conversation: Conversation,
        user_message: ConversationMessage,
        history: Sequence[ConversationMessage],
        emit: Any,
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
        result = await self._runtime.run(
            RunRequest(
                agent=conversation.agent,
                input=user_message.text_content,
                session_id=conversation.id,
                conversation_id=conversation.id,
                turn_id=user_message.id,
                tenant_id=conversation.tenant_id,
                user_id=conversation.user_id,
                metadata={"interface": "conversation"},
            )
        )
        if result.error:
            raise RuntimeError(result.error)
        return ConversationTurnResult(
            content=(TextBlock(text=result.output or ""),),
            run_ids=(result.run_id,),
            artifacts=result.artifacts,
            citations=tuple(item.model_dump(mode="json") for item in result.citations),
        )


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
        self._tracer = trace.get_tracer("algen_agent_runtime.conversations")
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
    ) -> tuple[ConversationMessage, ConversationMessage]:
        if not self._accepting:
            raise ConflictError("conversation service is draining")
        if not text.strip():
            raise ValueError("message cannot be empty")
        conversation = await self.get(conversation_id, tenant_id)
        if conversation.user_id != user_id:
            raise NotFoundError(f"conversation {conversation_id!r} not found")
        async with self._lock:
            active = self._tasks.get(conversation_id)
            if active and not active.done():
                raise ConflictError("conversation already has an active response")
            user_message = ConversationMessage.text(
                conversation_id=conversation.id,
                tenant_id=tenant_id,
                role=Role.USER,
                text=text.strip(),
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
            task = asyncio.create_task(self._respond(conversation, user_message, assistant_message))
            self._tasks[conversation.id] = task
            task.add_done_callback(self._forget_task)
            return user_message, assistant_message

    async def cancel(self, conversation_id: str, tenant_id: str) -> None:
        await self.get(conversation_id, tenant_id)
        task = self._tasks.get(conversation_id)
        if task and not task.done():
            task.cancel()

    async def recover(self, limit: int = 1000) -> int:
        """Fail interrupted responses safely instead of replaying unknown side effects."""
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
        return recovered

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

    async def _respond(
        self,
        conversation: Conversation,
        user_message: ConversationMessage,
        assistant_message: ConversationMessage,
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
        if self._telemetry_include_content:
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
                        conversation, user_message, assistant_message, trace_context
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
    ) -> tuple[str, tuple[str, ...], int, str]:
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
            await self._emit(conversation, event_type, data or {}, assistant_message.id)

        try:
            history = await self.store.messages(conversation.id, conversation.tenant_id, limit=1000)
            result = await self.handlers.get(conversation.handler).handle(
                conversation, user_message, history, emit
            )
            suggested_followups = result.suggested_followups
            if (
                not suggested_followups
                and result.outcome == "completed"
                and self._followup_provider is not None
            ):
                output_text = "\n".join(
                    str(getattr(block, "text", ""))
                    for block in result.content
                    if getattr(block, "text", None)
                )
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
            message_status = (
                MessageStatus.FAILED if result.outcome == "failed" else MessageStatus.COMPLETED
            )
            completed = streaming.model_copy(
                update={
                    "content": result.content,
                    "status": message_status,
                    "run_ids": result.run_ids,
                    "artifacts": result.artifacts,
                    "citations": result.citations,
                    "suggested_followups": suggested_followups,
                    "metadata": {
                        **redact(result.metadata),
                        **({"runtime_trace_context": trace_context} if trace_context else {}),
                    },
                    "updated_at": utc_now(),
                }
            )
            await self.store.update_message(completed)
            output_text = "\n".join(
                str(getattr(block, "text", ""))
                for block in result.content
                if getattr(block, "text", None)
            )
            event_payload = completed.model_dump(mode="json")
            if result.outcome == "failed":
                event_payload["message"] = output_text
            await emit(
                "assistant.message.failed"
                if result.outcome == "failed"
                else "assistant.message.completed",
                event_payload,
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
        if self._audits is not None:
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

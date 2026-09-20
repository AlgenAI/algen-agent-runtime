from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.events.contracts import AuditEvent
from algen_agent_runtime.exceptions.errors import NotFoundError
from algen_agent_runtime.security.redaction import redact
from algen_agent_runtime.types.contracts import utc_now


class FeedbackRating(StrEnum):
    UP = "up"
    DOWN = "down"


class ConversationFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str
    user_id: str
    conversation_id: str
    message_id: str
    rating: FeedbackRating
    category: str | None = Field(default=None, max_length=100)
    comment: str | None = Field(default=None, max_length=2_000)
    tags: tuple[str, ...] = Field(default=(), max_length=20)
    run_ids: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ConversationFeedbackStore(Protocol):
    async def put(self, feedback: ConversationFeedback) -> ConversationFeedback: ...
    async def get(
        self, tenant_id: str, user_id: str, message_id: str
    ) -> ConversationFeedback | None: ...
    async def list_for_conversation(
        self, tenant_id: str, user_id: str, conversation_id: str
    ) -> Sequence[ConversationFeedback]: ...


class InMemoryConversationFeedbackStore:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], ConversationFeedback] = {}
        self._lock = asyncio.Lock()

    async def put(self, feedback: ConversationFeedback) -> ConversationFeedback:
        key = (feedback.tenant_id, feedback.user_id, feedback.message_id)
        async with self._lock:
            current = self._items.get(key)
            saved = feedback.model_copy(
                update={
                    "id": current.id if current else feedback.id,
                    "created_at": current.created_at if current else feedback.created_at,
                    "updated_at": utc_now(),
                }
            )
            self._items[key] = saved
            return saved.model_copy(deep=True)

    async def get(
        self, tenant_id: str, user_id: str, message_id: str
    ) -> ConversationFeedback | None:
        async with self._lock:
            item = self._items.get((tenant_id, user_id, message_id))
            return item.model_copy(deep=True) if item else None

    async def list_for_conversation(
        self, tenant_id: str, user_id: str, conversation_id: str
    ) -> Sequence[ConversationFeedback]:
        async with self._lock:
            items = [
                item.model_copy(deep=True)
                for item in self._items.values()
                if item.tenant_id == tenant_id
                and item.user_id == user_id
                and item.conversation_id == conversation_id
            ]
        return tuple(sorted(items, key=lambda item: item.created_at))


class PostgresConversationFeedbackStore:
    def __init__(self, database: Any) -> None:
        self._database = database

    async def put(self, feedback: ConversationFeedback) -> ConversationFeedback:
        row = await self._database.fetchrow(
            "INSERT INTO algen_agent_runtime_conversation_feedback "
            "(id,tenant_id,user_id,conversation_id,message_id,feedback,created_at,updated_at) "
            "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8) "
            "ON CONFLICT (tenant_id,user_id,message_id) DO UPDATE SET "
            "feedback=EXCLUDED.feedback, updated_at=EXCLUDED.updated_at "
            "RETURNING id,created_at,updated_at",
            feedback.id,
            feedback.tenant_id,
            feedback.user_id,
            feedback.conversation_id,
            feedback.message_id,
            feedback.model_dump_json(),
            feedback.created_at,
            feedback.updated_at,
        )
        return feedback.model_copy(
            update={
                "id": str(row["id"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    async def get(
        self, tenant_id: str, user_id: str, message_id: str
    ) -> ConversationFeedback | None:
        row = await self._database.fetchrow(
            "SELECT feedback,id,created_at,updated_at FROM algen_agent_runtime_conversation_feedback "
            "WHERE tenant_id=$1 AND user_id=$2 AND message_id=$3",
            tenant_id,
            user_id,
            message_id,
        )
        if not row:
            return None
        return ConversationFeedback.model_validate_json(row["feedback"]).model_copy(
            update={
                "id": str(row["id"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    async def list_for_conversation(
        self, tenant_id: str, user_id: str, conversation_id: str
    ) -> Sequence[ConversationFeedback]:
        rows = await self._database.fetch(
            "SELECT feedback,id,created_at,updated_at FROM algen_agent_runtime_conversation_feedback "
            "WHERE tenant_id=$1 AND user_id=$2 AND conversation_id=$3 ORDER BY created_at",
            tenant_id,
            user_id,
            conversation_id,
        )
        return tuple(
            ConversationFeedback.model_validate_json(row["feedback"]).model_copy(
                update={
                    "id": str(row["id"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
            for row in rows
        )


def feedback_trace_context(metadata: dict[str, Any]) -> Context | None:
    value = metadata.get("runtime_trace_context")
    if not isinstance(value, dict):
        return None
    try:
        context = SpanContext(
            trace_id=int(str(value["trace_id"]), 16),
            span_id=int(str(value["span_id"]), 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return trace.set_span_in_context(NonRecordingSpan(context))


async def record_feedback(
    *,
    store: ConversationFeedbackStore,
    conversation: Any,
    message: Any,
    user_id: str,
    rating: FeedbackRating,
    category: str | None,
    comment: str | None,
    tags: tuple[str, ...],
    audits: Any | None,
) -> ConversationFeedback:
    if message.conversation_id != conversation.id or message.role.value != "assistant":
        raise NotFoundError(f"assistant message {message.id!r} not found")
    cleaned_tags = tuple(dict.fromkeys(tag.strip()[:100] for tag in tags if tag.strip()))[:20]
    feedback = ConversationFeedback(
        tenant_id=conversation.tenant_id,
        user_id=user_id,
        conversation_id=conversation.id,
        message_id=message.id,
        rating=rating,
        category=category.strip() if category and category.strip() else None,
        comment=str(redact(comment.strip())) if comment and comment.strip() else None,
        tags=cleaned_tags,
        run_ids=message.run_ids,
    )
    saved = await store.put(feedback)
    attributes: dict[str, Any] = {
        "span.type": "feedback",
        "feedback.id": saved.id,
        "feedback.rating": saved.rating.value,
        "feedback.message_id": saved.message_id,
        "feedback.conversation_id": saved.conversation_id,
        "feedback.run_ids": saved.run_ids,
        "tenant.id": saved.tenant_id,
        "user.id": saved.user_id,
    }
    if saved.category:
        attributes["feedback.category"] = saved.category
    tracer = trace.get_tracer("algen_agent_runtime.conversations.feedback")
    with tracer.start_as_current_span(
        "conversation.feedback",
        context=feedback_trace_context(message.metadata) or Context(),
        attributes=attributes,
    ):
        pass
    if audits is not None:
        await audits.append(
            AuditEvent(
                action="conversation.feedback.recorded",
                outcome="accepted",
                tenant_id=saved.tenant_id,
                actor_id=saved.user_id,
                resource_id=saved.message_id,
                metadata={"feedback_id": saved.id, "rating": saved.rating.value},
            )
        )
    return saved

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from algen_agent_runtime.conversations.contracts import (
    Conversation,
    ConversationEvent,
    ConversationMessage,
)
from algen_agent_runtime.conversations.feedback import (
    ConversationFeedback,
    FeedbackRating,
    PostgresConversationFeedbackStore,
)
from algen_agent_runtime.conversations.stores import (
    PostgresConversationEventBus,
    PostgresConversationStore,
)
from algen_agent_runtime.persistence.postgres import PostgresDatabase
from algen_agent_runtime.types.contracts import Role, TextBlock

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("POSTGRES_DSN"), reason="conversation deletion SQL needs PostgreSQL"
    ),
]


@pytest.fixture
async def database() -> AsyncIterator[PostgresDatabase]:
    pytest.importorskip("asyncpg")
    db = PostgresDatabase(os.environ["POSTGRES_DSN"])
    await db.start()
    try:
        yield db
    finally:
        await db.close()


async def _conversation_with_history(database: PostgresDatabase, tenant: str) -> Conversation:
    store = PostgresConversationStore(database)
    conversation = Conversation(tenant_id=tenant, user_id="u", agent="a")
    await store.create(conversation)
    message = ConversationMessage(
        conversation_id=conversation.id,
        tenant_id=tenant,
        role=Role.ASSISTANT,
        content=(TextBlock(text="hello"),),
    )
    await store.append_message(message)
    events = PostgresConversationEventBus(database)
    await events.publish(
        ConversationEvent(
            type="message.completed",
            conversation_id=conversation.id,
            tenant_id=tenant,
            message_id=message.id,
            sequence=await events.next_sequence(conversation.id),
        )
    )
    await PostgresConversationFeedbackStore(database).put(
        ConversationFeedback(
            tenant_id=tenant,
            user_id="u",
            conversation_id=conversation.id,
            message_id=message.id,
            rating=FeedbackRating.UP,
        )
    )
    return conversation


async def test_delete_removes_messages_events_and_feedback(database: PostgresDatabase) -> None:
    store = PostgresConversationStore(database)
    feedback = PostgresConversationFeedbackStore(database)
    conversation = await _conversation_with_history(database, "tenant-a")

    assert await store.delete(conversation.id, "tenant-a") is True

    assert await store.get(conversation.id, "tenant-a") is None
    assert await store.messages(conversation.id, "tenant-a", 10) == ()
    assert await PostgresConversationEventBus(database).history(conversation.id) == ()
    assert await feedback.list_for_conversation("tenant-a", "u", conversation.id) == ()
    assert await store.delete(conversation.id, "tenant-a") is False


async def test_delete_never_crosses_tenants(database: PostgresDatabase) -> None:
    store = PostgresConversationStore(database)
    conversation = await _conversation_with_history(database, "tenant-b")

    assert await store.delete(conversation.id, "tenant-other") is False
    assert await store.get(conversation.id, "tenant-b") is not None
    assert len(await store.messages(conversation.id, "tenant-b", 10)) == 1

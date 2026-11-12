"""Deleting a conversation deletes the session memory its turns wrote, owner included."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from conftest import make_runtime

from algen_agent_runtime.conversations.service import (
    ConversationHandlerRegistry,
    ConversationService,
    RuntimeConversationHandler,
)
from algen_agent_runtime.conversations.stores import (
    InMemoryConversationEventBus,
    InMemoryConversationStore,
)
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.persistence.memory import InMemoryMemoryStore
from algen_agent_runtime.types.contracts import Message, Role
from algen_agent_runtime.types.interfaces import SessionDeletion


async def _answered(conversations: ConversationService, conversation_id: str) -> None:
    for _ in range(400):
        messages = await conversations.messages(conversation_id, "t")
        if messages and messages[-1].status.value == "completed":
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the turn did not complete")


def _service(memory: object | None = None) -> tuple[ConversationService, object]:
    runtime = make_runtime(MockModelProvider(["the tool said 42"]))
    if memory is not None:
        runtime.memory = memory  # type: ignore[assignment]
    handlers = ConversationHandlerRegistry()
    handlers.register(RuntimeConversationHandler(runtime, stream=False))
    service = ConversationService(
        InMemoryConversationStore(), InMemoryConversationEventBus(), handlers
    )
    return service, runtime.memory


async def test_deleting_a_conversation_deletes_its_memory_session() -> None:
    conversations, memory = _service()
    assert isinstance(memory, InMemoryMemoryStore) and isinstance(memory, SessionDeletion)
    conversation = await conversations.create(tenant_id="t", user_id="alice", agent="test-agent")
    await conversations.submit(conversation.id, "t", "alice", "what did the tool say?")
    await _answered(conversations, conversation.id)
    assert await memory.get("t", conversation.id, 10)
    assert await memory.session_owner("t", conversation.id) == "alice"
    await memory.append("t", "other-session", [Message.text(Role.USER, "keep me")])

    await conversations.delete(conversation.id, "t", "alice")

    assert await memory.get("t", conversation.id, 10) == ()
    assert await memory.session_owner("t", conversation.id) is None
    assert await memory.get("t", "other-session", 10)


class _PlainMemory(InMemoryMemoryStore):
    """A memory store with only the MemoryStore protocol's delete."""

    deleted: list[str]

    def __init__(self) -> None:
        super().__init__()
        self.deleted = []

    delete_session = None  # type: ignore[assignment]

    async def delete(self, tenant_id: str, session_id: str) -> None:
        self.deleted.append(session_id)
        await super().delete(tenant_id, session_id)


async def test_a_store_without_delete_session_gets_delete() -> None:
    plain = _PlainMemory()
    assert not isinstance(plain, SessionDeletion)
    conversations, _ = _service(plain)
    conversation = await conversations.create(tenant_id="t", user_id="alice", agent="test-agent")
    await conversations.submit(conversation.id, "t", "alice", "hi")
    await _answered(conversations, conversation.id)

    await conversations.delete(conversation.id, "t", "alice")

    assert plain.deleted == [conversation.id]
    assert await plain.get("t", conversation.id, 10) == ()


async def test_delete_session_counts_messages_and_is_idempotent() -> None:
    memory = InMemoryMemoryStore()
    assert await memory.claim_session("t", "s", "alice")
    messages: Sequence[Message] = [Message.text(Role.USER, "a"), Message.text(Role.USER, "b")]
    await memory.append("t", "s", messages)

    assert await memory.delete_session("t", "s") == 2
    assert await memory.delete_session("t", "s") == 0
    assert await memory.claim_session("t", "s", "bob")  # the session is free again

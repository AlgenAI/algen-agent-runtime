from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any

from algen_agent_runtime.conversations.contracts import (
    Conversation,
    ConversationEvent,
    ConversationMessage,
    MessageStatus,
)
from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.types.contracts import utc_now


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._conversations: dict[str, Conversation] = {}
        self._messages: dict[str, list[ConversationMessage]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def create(self, conversation: Conversation) -> None:
        async with self._lock:
            if conversation.id in self._conversations:
                raise ConflictError(f"conversation {conversation.id!r} already exists")
            self._conversations[conversation.id] = conversation.model_copy(deep=True)

    async def get(self, conversation_id: str, tenant_id: str) -> Conversation | None:
        async with self._lock:
            item = self._conversations.get(conversation_id)
            if item is None or item.tenant_id != tenant_id:
                return None
            return item.model_copy(deep=True)

    async def list(
        self, tenant_id: str, user_id: str, limit: int, before: datetime | None = None
    ) -> Sequence[Conversation]:
        async with self._lock:
            items = [
                item.model_copy(deep=True)
                for item in self._conversations.values()
                if item.tenant_id == tenant_id
                and item.user_id == user_id
                and (before is None or item.updated_at < before)
            ]
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return tuple(items[:limit])

    async def save(self, conversation: Conversation, expected_version: int) -> None:
        async with self._lock:
            current = self._conversations.get(conversation.id)
            if current is None or current.version != expected_version:
                raise ConflictError(f"conversation {conversation.id!r} was concurrently modified")
            saved = conversation.model_copy(deep=True)
            saved.version = expected_version + 1
            conversation.version = saved.version
            self._conversations[conversation.id] = saved

    async def append_message(self, message: ConversationMessage) -> None:
        async with self._lock:
            if any(item.id == message.id for item in self._messages[message.conversation_id]):
                raise ConflictError(f"message {message.id!r} already exists")
            self._messages[message.conversation_id].append(message.model_copy(deep=True))

    async def update_message(self, message: ConversationMessage) -> None:
        async with self._lock:
            messages = self._messages[message.conversation_id]
            for index, current in enumerate(messages):
                if current.id == message.id and current.tenant_id == message.tenant_id:
                    messages[index] = message.model_copy(deep=True)
                    return
            raise ConflictError(f"message {message.id!r} was not found")

    async def messages(
        self,
        conversation_id: str,
        tenant_id: str,
        limit: int,
        before: datetime | None = None,
    ) -> Sequence[ConversationMessage]:
        async with self._lock:
            items = [
                item.model_copy(deep=True)
                for item in self._messages.get(conversation_id, ())
                if item.tenant_id == tenant_id and (before is None or item.created_at < before)
            ]
        return tuple(items[-limit:])

    async def pending_messages(self, limit: int = 1000) -> Sequence[ConversationMessage]:
        async with self._lock:
            pending = [
                item.model_copy(deep=True)
                for messages in self._messages.values()
                for item in messages
                if item.role.value == "assistant"
                and item.status in {MessageStatus.ACCEPTED, MessageStatus.STREAMING}
            ]
        pending.sort(key=lambda item: item.created_at)
        return tuple(pending[:limit])


class InMemoryConversationEventBus:
    def __init__(self, history_limit: int = 5000, queue_size: int = 1000) -> None:
        self._history: dict[str, list[ConversationEvent]] = defaultdict(list)
        self._subscribers: dict[str, set[asyncio.Queue[ConversationEvent]]] = defaultdict(set)
        self._history_limit = history_limit
        self._queue_size = queue_size
        self._lock = asyncio.Lock()

    async def next_sequence(self, conversation_id: str) -> int:
        async with self._lock:
            history = self._history.get(conversation_id, ())
            return history[-1].sequence + 1 if history else 1

    async def publish(self, event: ConversationEvent) -> None:
        async with self._lock:
            history = self._history[event.conversation_id]
            history.append(event)
            del history[: -self._history_limit]
            subscribers = tuple(self._subscribers[event.conversation_id])
        for queue in subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    async def history(self, conversation_id: str, after: int = 0) -> Sequence[ConversationEvent]:
        async with self._lock:
            return tuple(
                event for event in self._history.get(conversation_id, ()) if event.sequence > after
            )

    async def subscribe(
        self, conversation_id: str, after: int = 0
    ) -> AsyncIterator[ConversationEvent]:
        queue: asyncio.Queue[ConversationEvent] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._subscribers[conversation_id].add(queue)
        try:
            cursor = after
            for event in await self.history(conversation_id, cursor):
                cursor = max(cursor, event.sequence)
                yield event
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.5)
                    if event.sequence > cursor:
                        cursor = event.sequence
                        yield event
                except TimeoutError:
                    for event in await self.history(conversation_id, cursor):
                        cursor = max(cursor, event.sequence)
                        yield event
        finally:
            async with self._lock:
                self._subscribers[conversation_id].discard(queue)


class PostgresConversationStore:
    def __init__(self, database: Any) -> None:
        self._database = database

    async def create(self, conversation: Conversation) -> None:
        await self._database.execute(
            "INSERT INTO algen_agent_runtime_conversations "
            "(id,tenant_id,user_id,version,conversation,created_at,updated_at) "
            "VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7)",
            conversation.id,
            conversation.tenant_id,
            conversation.user_id,
            conversation.version,
            conversation.model_dump_json(),
            conversation.created_at,
            conversation.updated_at,
        )

    async def get(self, conversation_id: str, tenant_id: str) -> Conversation | None:
        row = await self._database.fetchrow(
            "SELECT conversation FROM algen_agent_runtime_conversations WHERE id=$1 AND tenant_id=$2",
            conversation_id,
            tenant_id,
        )
        return Conversation.model_validate_json(row["conversation"]) if row else None

    async def list(
        self, tenant_id: str, user_id: str, limit: int, before: datetime | None = None
    ) -> Sequence[Conversation]:
        rows = await self._database.fetch(
            "SELECT conversation FROM algen_agent_runtime_conversations "
            "WHERE tenant_id=$1 AND user_id=$2 AND ($3::timestamptz IS NULL OR updated_at<$3) "
            "ORDER BY updated_at DESC LIMIT $4",
            tenant_id,
            user_id,
            before,
            limit,
        )
        return tuple(Conversation.model_validate_json(row["conversation"]) for row in rows)

    async def save(self, conversation: Conversation, expected_version: int) -> None:
        saved = conversation.model_copy(
            update={"version": expected_version + 1, "updated_at": utc_now()}
        )
        result = await self._database.execute(
            "UPDATE algen_agent_runtime_conversations SET version=$1, conversation=$2::jsonb, "
            "updated_at=$3 WHERE id=$4 AND tenant_id=$5 AND version=$6",
            saved.version,
            saved.model_dump_json(),
            saved.updated_at,
            saved.id,
            saved.tenant_id,
            expected_version,
        )
        if result != "UPDATE 1":
            raise ConflictError(f"conversation {conversation.id!r} was concurrently modified")
        conversation.version = saved.version
        conversation.updated_at = saved.updated_at

    async def append_message(self, message: ConversationMessage) -> None:
        await self._database.execute(
            "INSERT INTO algen_agent_runtime_conversation_messages "
            "(id,conversation_id,tenant_id,message,created_at) VALUES ($1,$2,$3,$4::jsonb,$5)",
            message.id,
            message.conversation_id,
            message.tenant_id,
            message.model_dump_json(),
            message.created_at,
        )

    async def update_message(self, message: ConversationMessage) -> None:
        result = await self._database.execute(
            "UPDATE algen_agent_runtime_conversation_messages SET message=$1::jsonb "
            "WHERE id=$2 AND conversation_id=$3 AND tenant_id=$4",
            message.model_dump_json(),
            message.id,
            message.conversation_id,
            message.tenant_id,
        )
        if result != "UPDATE 1":
            raise ConflictError(f"message {message.id!r} was not found")

    async def messages(
        self,
        conversation_id: str,
        tenant_id: str,
        limit: int,
        before: datetime | None = None,
    ) -> Sequence[ConversationMessage]:
        rows = await self._database.fetch(
            "SELECT message FROM algen_agent_runtime_conversation_messages "
            "WHERE conversation_id=$1 AND tenant_id=$2 "
            "AND ($3::timestamptz IS NULL OR created_at<$3) "
            "ORDER BY ordinal DESC LIMIT $4",
            conversation_id,
            tenant_id,
            before,
            limit,
        )
        return tuple(
            ConversationMessage.model_validate_json(row["message"]) for row in reversed(rows)
        )

    async def pending_messages(self, limit: int = 1000) -> Sequence[ConversationMessage]:
        rows = await self._database.fetch(
            "SELECT message FROM algen_agent_runtime_conversation_messages "
            "WHERE message->>'role'='assistant' "
            "AND message->>'status' IN ('accepted','streaming') ORDER BY created_at LIMIT $1",
            limit,
        )
        return tuple(ConversationMessage.model_validate_json(row["message"]) for row in rows)


class PostgresConversationEventBus:
    def __init__(self, database: Any, queue_size: int = 1000) -> None:
        self._database = database
        self._queue_size = queue_size
        self._subscribers: dict[str, set[asyncio.Queue[ConversationEvent]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def next_sequence(self, conversation_id: str) -> int:
        row = await self._database.fetchrow(
            "SELECT COALESCE(MAX(sequence),0)+1 AS sequence "
            "FROM algen_agent_runtime_conversation_events WHERE conversation_id=$1",
            conversation_id,
        )
        return int(row["sequence"])

    async def publish(self, event: ConversationEvent) -> None:
        await self._database.execute(
            "INSERT INTO algen_agent_runtime_conversation_events "
            "(id,conversation_id,tenant_id,sequence,event,created_at) "
            "VALUES ($1,$2,$3,$4,$5::jsonb,$6)",
            event.id,
            event.conversation_id,
            event.tenant_id,
            event.sequence,
            event.model_dump_json(),
            event.timestamp,
        )
        async with self._lock:
            subscribers = tuple(self._subscribers[event.conversation_id])
        for queue in subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    async def history(self, conversation_id: str, after: int = 0) -> Sequence[ConversationEvent]:
        rows = await self._database.fetch(
            "SELECT event FROM algen_agent_runtime_conversation_events "
            "WHERE conversation_id=$1 AND sequence>$2 ORDER BY sequence",
            conversation_id,
            after,
        )
        return tuple(ConversationEvent.model_validate_json(row["event"]) for row in rows)

    async def subscribe(
        self, conversation_id: str, after: int = 0
    ) -> AsyncIterator[ConversationEvent]:
        queue: asyncio.Queue[ConversationEvent] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._subscribers[conversation_id].add(queue)
        try:
            for event in await self.history(conversation_id, after):
                yield event
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers[conversation_id].discard(queue)

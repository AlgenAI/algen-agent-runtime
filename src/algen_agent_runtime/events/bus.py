from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from algen_agent_runtime.events.contracts import AuditEvent, RunEvent


class InMemoryEventBus:
    def __init__(self, history_limit: int = 1000) -> None:
        self._history: dict[str, list[RunEvent]] = defaultdict(list)
        self._subscribers: dict[str, set[asyncio.Queue[RunEvent]]] = defaultdict(set)
        self._history_limit = history_limit
        self._lock = asyncio.Lock()

    async def next_sequence(self, run_id: str) -> int:
        async with self._lock:
            history = self._history.get(run_id, ())
            return history[-1].sequence + 1 if history else 1

    async def publish(self, event: RunEvent) -> None:
        async with self._lock:
            history = self._history[event.run_id]
            history.append(event)
            del history[: -self._history_limit]
            subscribers = tuple(self._subscribers[event.run_id])
        for queue in subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    async def history(self, run_id: str, after: int = 0) -> tuple[RunEvent, ...]:
        async with self._lock:
            return tuple(event for event in self._history.get(run_id, ()) if event.sequence > after)

    async def subscribe(self, run_id: str, after: int = 0) -> AsyncIterator[RunEvent]:
        queue: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subscribers[run_id].add(queue)
            backlog = tuple(
                event for event in self._history.get(run_id, ()) if event.sequence > after
            )
        try:
            for event in backlog:
                yield event
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers[run_id].discard(queue)


class InMemoryAuditLog:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = asyncio.Lock()

    async def append(self, event: AuditEvent) -> None:
        async with self._lock:
            self._events.append(event)

    async def list(self, tenant_id: str, resource_id: str | None = None) -> tuple[AuditEvent, ...]:
        async with self._lock:
            return tuple(
                event
                for event in self._events
                if event.tenant_id == tenant_id
                and (resource_id is None or event.resource_id == resource_id)
            )


class PostgresEventBus:
    """Durable event history with process-local live fan-out for SSE clients."""

    def __init__(self, database: Any, subscriber_queue_size: int = 1000) -> None:
        self._database = database
        self._subscribers: dict[str, set[asyncio.Queue[RunEvent]]] = defaultdict(set)
        self._subscriber_queue_size = subscriber_queue_size
        self._lock = asyncio.Lock()

    async def next_sequence(self, run_id: str) -> int:
        row = await self._database.fetchrow(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence "
            "FROM algen_agent_runtime_events WHERE run_id=$1",
            run_id,
        )
        return int(row["sequence"])

    async def publish(self, event: RunEvent) -> None:
        await self._database.execute(
            "INSERT INTO algen_agent_runtime_events "
            "(id, run_id, tenant_id, sequence, event, created_at) "
            "VALUES ($1,$2,$3,$4,$5::jsonb,$6)",
            event.id,
            event.run_id,
            event.tenant_id,
            event.sequence,
            event.model_dump_json(),
            event.timestamp,
        )
        async with self._lock:
            subscribers = tuple(self._subscribers[event.run_id])
        for queue in subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    async def history(self, run_id: str, after: int = 0) -> tuple[RunEvent, ...]:
        rows = await self._database.fetch(
            "SELECT event FROM algen_agent_runtime_events "
            "WHERE run_id=$1 AND sequence>$2 ORDER BY sequence",
            run_id,
            after,
        )
        return tuple(RunEvent.model_validate_json(row["event"]) for row in rows)

    async def subscribe(self, run_id: str, after: int = 0) -> AsyncIterator[RunEvent]:
        queue: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=self._subscriber_queue_size)
        async with self._lock:
            self._subscribers[run_id].add(queue)
        try:
            cursor = after
            for event in await self.history(run_id, cursor):
                cursor = max(cursor, event.sequence)
                yield event
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.5)
                    if event.sequence > cursor:
                        cursor = event.sequence
                        yield event
                except TimeoutError:
                    # Durable polling makes events emitted by another process visible
                    # without requiring a particular PostgreSQL notification topology.
                    for event in await self.history(run_id, cursor):
                        cursor = max(cursor, event.sequence)
                        yield event
        finally:
            async with self._lock:
                self._subscribers[run_id].discard(queue)


class PostgresAuditLog:
    def __init__(self, database: Any) -> None:
        self._database = database

    async def append(self, event: AuditEvent) -> None:
        await self._database.execute(
            "INSERT INTO algen_agent_runtime_audit_events "
            "(id, tenant_id, event, created_at) VALUES ($1,$2,$3::jsonb,$4)",
            event.id,
            event.tenant_id,
            event.model_dump_json(),
            event.timestamp,
        )

    async def list(self, tenant_id: str, resource_id: str | None = None) -> tuple[AuditEvent, ...]:
        rows = await self._database.fetch(
            "SELECT event FROM algen_agent_runtime_audit_events WHERE tenant_id=$1 "
            "AND ($2::text IS NULL OR event->>'resource_id'=$2) ORDER BY created_at",
            tenant_id,
            resource_id,
        )
        return tuple(AuditEvent.model_validate_json(row["event"]) for row in rows)

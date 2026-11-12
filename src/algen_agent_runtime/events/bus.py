from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from typing import Any

from algen_agent_runtime.events.contracts import AuditEvent, RunEvent
from algen_agent_runtime.exceptions.errors import EventStreamOverflowError, RunConflictError


def _is_sequence_conflict(exc: BaseException) -> bool:
    """Whether a unique violation is on the events' ``(run_id, sequence)`` key, not their id."""
    constraint = getattr(exc, "constraint_name", None)
    if isinstance(constraint, str):
        return "pkey" not in constraint
    return "sequence" in str(exc)


class _Subscription:
    """One subscriber's live queue.

    A publish that finds the queue full marks the subscription ``overflowed`` and stops adding
    to it, instead of dropping an event the subscriber would never know it missed: once the
    subscriber has read what is queued, it reads on from the bus's history after the last event
    it received.
    """

    __slots__ = ("overflowed", "queue")

    def __init__(self, size: int) -> None:
        self.queue: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=size)
        self.overflowed = False

    def offer(self, event: RunEvent) -> None:
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.overflowed = True

    @property
    def behind(self) -> bool:
        """Whether the subscriber must read on from the history: it overflowed and has read
        everything that was queued before."""
        return self.overflowed and self.queue.empty()


class InMemoryEventBus:
    """Run events in process memory: the last ``history_limit`` events of each run, and live
    fan-out to subscribers with queues of ``subscriber_queue_size`` events.

    A subscriber that falls more than its queue behind reads on from the history; when the
    history no longer holds the events it missed, its subscription ends with
    ``EventStreamOverflowError`` rather than skipping them.
    """

    def __init__(self, history_limit: int = 1000, subscriber_queue_size: int = 1000) -> None:
        self._history: dict[str, list[RunEvent]] = defaultdict(list)
        self._subscribers: dict[str, set[_Subscription]] = defaultdict(set)
        self._history_limit = history_limit
        self._subscriber_queue_size = subscriber_queue_size
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
        for subscription in subscribers:
            subscription.offer(event)

    async def history(self, run_id: str, after: int = 0) -> tuple[RunEvent, ...]:
        async with self._lock:
            return tuple(event for event in self._history.get(run_id, ()) if event.sequence > after)

    async def forget_runs(self, run_ids: Sequence[str]) -> int:
        """Drop the event history of these runs; return how many events went."""
        async with self._lock:
            return sum(len(self._history.pop(run_id, ())) for run_id in set(run_ids))

    async def subscribe(self, run_id: str, after: int = 0) -> AsyncIterator[RunEvent]:
        """Yield the run's events after sequence ``after``, then live ones, in order.

        Raises ``EventStreamOverflowError`` (with the last delivered sequence) when this
        subscriber fell behind further than the kept history reaches.
        """
        subscription = _Subscription(self._subscriber_queue_size)
        async with self._lock:
            self._subscribers[run_id].add(subscription)
            backlog = tuple(
                event for event in self._history.get(run_id, ()) if event.sequence > after
            )
        cursor = after
        try:
            for event in backlog:
                cursor = max(cursor, event.sequence)
                yield event
            while True:
                if subscription.behind:
                    async with self._lock:
                        subscription.overflowed = False
                        missed = tuple(
                            event
                            for event in self._history.get(run_id, ())
                            if event.sequence > cursor
                        )
                    if missed and missed[0].sequence > cursor + 1:
                        raise EventStreamOverflowError(run_id, cursor)
                    for event in missed:
                        if event.sequence > cursor:
                            cursor = event.sequence
                            yield event
                    continue
                event = await subscription.queue.get()
                if event.sequence > cursor:
                    cursor = event.sequence
                    yield event
        finally:
            async with self._lock:
                self._subscribers[run_id].discard(subscription)


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
        self._subscribers: dict[str, set[_Subscription]] = defaultdict(set)
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
        """Record ``event``; ``RunConflictError`` when another writer already took its
        sequence number (another process driving the same run)."""
        try:
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
        except Exception as exc:
            if "UniqueViolationError" in type(exc).__name__ and _is_sequence_conflict(exc):
                raise RunConflictError(
                    f"event {event.sequence} of run {event.run_id!r} was already written by "
                    "another writer"
                ) from exc
            raise
        async with self._lock:
            subscribers = tuple(self._subscribers[event.run_id])
        for subscription in subscribers:
            subscription.offer(event)

    async def history(self, run_id: str, after: int = 0) -> tuple[RunEvent, ...]:
        rows = await self._database.fetch(
            "SELECT event FROM algen_agent_runtime_events "
            "WHERE run_id=$1 AND sequence>$2 ORDER BY sequence",
            run_id,
            after,
        )
        return tuple(RunEvent.model_validate_json(row["event"]) for row in rows)

    async def forget_runs(self, run_ids: Sequence[str]) -> int:
        """Delete the event history of these runs; return how many events went."""
        if not run_ids:
            return 0
        result = await self._database.execute(
            "DELETE FROM algen_agent_runtime_events WHERE run_id = ANY($1::text[])", list(run_ids)
        )
        return int(str(result).rsplit(" ", 1)[-1])

    async def subscribe(self, run_id: str, after: int = 0) -> AsyncIterator[RunEvent]:
        """Yield the run's events after sequence ``after``, then live ones, in order.

        A subscriber that falls more than its queue behind reads on from the durable history,
        so it misses nothing.
        """
        subscription = _Subscription(self._subscriber_queue_size)
        queue = subscription.queue
        async with self._lock:
            self._subscribers[run_id].add(subscription)
        try:
            cursor = after
            for event in await self.history(run_id, cursor):
                cursor = max(cursor, event.sequence)
                yield event
            while True:
                if subscription.behind:
                    subscription.overflowed = False
                    for event in await self.history(run_id, cursor):
                        if event.sequence > cursor:
                            cursor = event.sequence
                            yield event
                    continue
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
                self._subscribers[run_id].discard(subscription)


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

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from opentelemetry import metrics

from algen_agent_runtime.distributed.contracts import WorkItem, WorkQueue
from algen_agent_runtime.observability.safe_spans import record_exception, runtime_tracer
from algen_agent_runtime.security.redaction import redact_error_text

WorkHandler = Callable[[WorkItem], Awaitable[dict[str, Any]]]


class DistributedWorker:
    """Experimental: horizontal scaling; not covered by the compatibility promise."""

    def __init__(self, worker_id: str, queue: WorkQueue, *, lease_seconds: int = 30) -> None:
        self.worker_id = worker_id
        self._queue = queue
        self._lease_seconds = lease_seconds
        self._handlers: dict[str, WorkHandler] = {}
        self._tracer = runtime_tracer("algen_agent_runtime.distributed")
        self._counter = metrics.get_meter("algen_agent_runtime.distributed").create_counter(
            "algen_agent_runtime.work.executions"
        )

    def register(self, kind: str, handler: WorkHandler) -> None:
        if kind in self._handlers:
            raise ValueError(f"work handler {kind!r} already exists")
        self._handlers[kind] = handler

    async def run_once(self) -> bool:
        item = await self._queue.claim(self.worker_id, self._lease_seconds)
        if item is None:
            return False
        with self._tracer.start_as_current_span(
            "distributed_work.execute",
            attributes={"work.id": item.id, "work.kind": item.kind, "tenant.id": item.tenant_id},
        ) as span:
            try:
                handler = self._handlers[item.kind]
                result = await handler(item)
                await self._queue.complete(item.id, self.worker_id, result)
                self._counter.add(1, {"work.kind": item.kind, "outcome": "completed"})
            except asyncio.CancelledError:
                await self._queue.fail(item.id, self.worker_id, "worker cancelled", True)
                raise
            except Exception as exc:
                record_exception(span, exc, status=False)
                # The queue keeps the error in its row: masked (0.2.0b5).
                await self._queue.fail(item.id, self.worker_id, redact_error_text(exc), True)
                self._counter.add(1, {"work.kind": item.kind, "outcome": "failed"})
        return True

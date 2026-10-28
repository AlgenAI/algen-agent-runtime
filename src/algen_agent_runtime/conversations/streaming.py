"""Forward a run's model text deltas to a conversation turn, at a bounded rate."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from algen_agent_runtime.conversations.contracts import DeltaEmitter
from algen_agent_runtime.types.contracts import RunResult
from algen_agent_runtime.types.interfaces import Runtime

DELTA_EVENT = "assistant.message.delta"


class DeltaCoalescer:
    """Buffer small text deltas and emit them as ``assistant.message.delta`` events.

    A turn emits at most one delta event per ``interval_seconds``, except that a buffer reaching
    ``max_chars`` is sent at once. Each event carries at most ``max_chars`` characters, so a turn
    sends no more than ``1 / interval_seconds`` events per second plus one per ``max_chars``
    characters of model output. Buffered text is flushed before the turn pauses or ends.
    """

    def __init__(self, emit: DeltaEmitter, *, interval_seconds: float, max_chars: int) -> None:
        if interval_seconds < 0:
            raise ValueError("interval_seconds cannot be negative")
        if max_chars < 1:
            raise ValueError("max_chars must be at least 1")
        self._emit = emit
        self._interval = interval_seconds
        self._max_chars = max_chars
        self._parts: list[str] = []
        self._size = 0
        self._last_flush = time.monotonic()
        self.events = 0

    async def add(self, text: str) -> None:
        if not text:
            return
        self._parts.append(text)
        self._size += len(text)
        if self._size >= self._max_chars or self.timeout() == 0:
            await self.flush()

    def timeout(self) -> float | None:
        """Seconds until buffered text is due, or None when nothing is buffered."""
        if not self._parts:
            return None
        return max(0.0, self._interval - (time.monotonic() - self._last_flush))

    async def flush(self) -> None:
        if not self._parts:
            return
        text = "".join(self._parts)
        self._parts.clear()
        self._size = 0
        self._last_flush = time.monotonic()
        for start in range(0, len(text), self._max_chars):
            self.events += 1
            await self._emit(DELTA_EVENT, {"delta": text[start : start + self._max_chars]})


async def follow_run(
    runtime: Runtime,
    run_id: str,
    tenant_id: str,
    coalescer: DeltaCoalescer | None,
    *,
    after: int = 0,
) -> RunResult:
    """Wait for the run's current drive and forward its ``model.delta`` events meanwhile.

    It returns when the run pauses or ends. Only text deltas are forwarded: never tool calls,
    arguments or results. Cancelling it cancels the wait, which cancels the run's drive as
    ``Runtime.run`` did.
    """
    events: Any = getattr(runtime, "events", None)
    if coalescer is None or events is None:
        return await runtime.wait(run_id, tenant_id)
    queue: asyncio.Queue[Any] = asyncio.Queue()
    cursor = after

    async def pump() -> None:
        async for event in events.subscribe(run_id, after):
            queue.put_nowait(event)

    async def forward(event: Any) -> None:
        nonlocal cursor
        if event.sequence <= cursor:
            return
        cursor = event.sequence
        if event.type == "model.delta":
            await coalescer.add(str(event.data.get("delta", "")))

    pump_task = asyncio.create_task(pump())
    wait_task = asyncio.create_task(runtime.wait(run_id, tenant_id))
    getter: asyncio.Task[Any] | None = None
    try:
        while not wait_task.done():
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {getter, wait_task},
                timeout=coalescer.timeout(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if getter in done:
                await forward(getter.result())
            else:
                getter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await getter
            getter = None
            if not done:
                await coalescer.flush()
        while not queue.empty():
            await forward(queue.get_nowait())
        for event in await events.history(run_id, cursor):
            await forward(event)
        await coalescer.flush()
        return wait_task.result()
    except asyncio.CancelledError:
        wait_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await wait_task
        raise
    finally:
        for task in (getter, pump_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(pump_task, return_exceptions=True)
        if getter is not None:
            await asyncio.gather(getter, return_exceptions=True)

"""Forward a run's model text deltas to a conversation turn, at a bounded rate."""

from __future__ import annotations

import asyncio
import contextlib
import time
from contextvars import ContextVar
from typing import Any

from algen_agent_runtime.conversations.contracts import DeltaEmitter
from algen_agent_runtime.types.contracts import RunResult
from algen_agent_runtime.types.interfaces import Runtime

DELTA_EVENT = "assistant.message.delta"

DETACH_SIGNAL: ContextVar[asyncio.Event | None] = ContextVar(
    "algen_agent_runtime_conversation_detach", default=None
)
"""Set by the conversation service on each turn's task: once the event is set (a shutdown),
cancelling the turn detaches it from its run instead of cancelling the run."""


def detaching() -> bool:
    """Whether the current turn is being detached from its run rather than cancelled."""
    signal = DETACH_SIGNAL.get()
    return signal is not None and signal.is_set()


RUN_STARTED_EVENT = "agent.run.started"
"""Emitted by a handler with ``{"run_id": ...}`` once a turn's run exists; the service stores the
id on the assistant message at once, so a restarted service can follow the run."""
TOOL_STARTED_EVENT = "agent.tool.started"
TOOL_COMPLETED_EVENT = "agent.tool.completed"
STEER_DELIVERED_EVENT = "agent.steer.delivered"
STEER_UNDELIVERED_EVENT = "agent.steer.undelivered"
_STEER_EVENTS = {
    "steer.delivered": STEER_DELIVERED_EVENT,
    "steer.undelivered": STEER_UNDELIVERED_EVENT,
}

# The run event fields a tool step carries: names, statuses and sizes, never arguments or results.
_TOOL_STEP_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "tool.started": (TOOL_STARTED_EVENT, ("tool", "status", "side_effect", "argument_keys")),
    "tool.completed": (
        TOOL_COMPLETED_EVENT,
        ("tool", "status", "side_effect", "result_redacted", "result_chars", "artifact_count"),
    ),
}


def steering_step(event: Any) -> tuple[str, dict[str, Any]] | None:
    """The conversation event for a run's ``steer.delivered`` or ``steer.undelivered``: the
    steering message ids and sizes, never their text."""
    event_type = _STEER_EVENTS.get(str(event.type))
    if event_type is None:
        return None
    messages = event.data.get("messages") or []
    return event_type, {
        "count": int(event.data.get("count", len(messages))),
        "messages": [
            {"id": item.get("id"), "chars": item.get("chars")}
            for item in messages
            if isinstance(item, dict)
        ],
    }


def tool_step(event: Any) -> tuple[str, dict[str, Any]] | None:
    """The conversation event for a run's ``tool.started`` or ``tool.completed``, or None."""
    mapped = _TOOL_STEP_FIELDS.get(str(event.type))
    if mapped is None:
        return None
    event_type, fields = mapped
    data = {key: event.data[key] for key in fields if key in event.data}
    step_id = getattr(event, "step_id", None)
    return event_type, {"call_id": str(step_id).removeprefix("tool-") if step_id else None, **data}


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
    steps: DeltaEmitter | None = None,
    tool_steps: bool = True,
) -> RunResult:
    """Wait for the run's current drive and forward its ``model.delta`` events meanwhile.

    It returns when the run pauses or ends. Text deltas go to ``coalescer``. ``steps`` gets each
    ``steer.delivered`` and ``steer.undelivered`` as ``agent.steer.delivered`` or
    ``agent.steer.undelivered`` (message ids and sizes), and, with ``tool_steps``, each
    ``tool.started`` and ``tool.completed`` as ``agent.tool.started`` or ``agent.tool.completed``
    with the tool's name, status, side effect, argument names and result size only: never
    arguments or results. Cancelling it cancels the wait, which cancels the
    run's drive as ``Runtime.run`` did, except while the service shuts down (:func:`detaching`):
    then the run keeps going, for the Runtime to save for recovery.
    """
    events: Any = getattr(runtime, "events", None)
    if (coalescer is None and steps is None) or events is None:
        waiting = asyncio.create_task(runtime.wait(run_id, tenant_id))
        try:
            return await asyncio.shield(waiting)
        except asyncio.CancelledError:
            if not detaching():
                waiting.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await waiting
            raise
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
            if coalescer is not None:
                await coalescer.add(str(event.data.get("delta", "")))
        elif steps is not None and (
            (step := steering_step(event)) is not None
            or (tool_steps and (step := tool_step(event)) is not None)
        ):
            if coalescer is not None:
                await coalescer.flush()  # keep text and tool steps in the order they happened
            await steps(*step)

    pump_task = asyncio.create_task(pump())
    wait_task = asyncio.create_task(runtime.wait(run_id, tenant_id))
    getter: asyncio.Task[Any] | None = None
    try:
        while not wait_task.done():
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {getter, wait_task},
                timeout=coalescer.timeout() if coalescer is not None else None,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if getter in done:
                await forward(getter.result())
            else:
                getter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await getter
            getter = None
            if not done and coalescer is not None:
                await coalescer.flush()
        while not queue.empty():
            await forward(queue.get_nowait())
        for event in await events.history(run_id, cursor):
            await forward(event)
        if coalescer is not None:
            await coalescer.flush()
        return wait_task.result()
    except asyncio.CancelledError:
        if not detaching():
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

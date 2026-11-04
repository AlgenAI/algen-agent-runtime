"""Blocking tool code in worker threads that the executor can account for.

A synchronous tool runs in a worker thread so it cannot block the event loop. A caller that stops
waiting (a timeout) cannot stop the thread, so the executor keeps the tool's concurrency slot until
every thread the call started has finished: ``max_concurrency`` bounds the work actually running,
not only the callers still waiting.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_threads: ContextVar[list[asyncio.Future[Any]] | None] = ContextVar(
    "algen_agent_runtime_tool_threads", default=None
)


async def run_blocking[T](function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run ``function`` in a worker thread and wait for it.

    Cancelling the wait (a timeout) leaves the thread running; the tool call that started it is
    told, through :func:`track_threads`, so it can hold its capacity until the thread finishes.
    """
    work = asyncio.ensure_future(asyncio.to_thread(function, *args, **kwargs))
    work.add_done_callback(_retrieve)
    threads = _threads.get()
    if threads is not None:
        threads.append(work)
    return await asyncio.shield(work)


@contextmanager
def track_threads() -> Iterator[list[asyncio.Future[Any]]]:
    """Collect the worker threads :func:`run_blocking` starts within the block."""
    threads: list[asyncio.Future[Any]] = []
    token = _threads.set(threads)
    try:
        yield threads
    finally:
        _threads.reset(token)


def _retrieve(work: asyncio.Future[Any]) -> None:
    # A thread nobody waits for any more may still fail; retrieving its error keeps asyncio from
    # logging "exception was never retrieved". The executor already recorded the call's outcome.
    if not work.cancelled():
        work.exception()

"""Blocking tool code in a bounded pool of worker threads that the executor can account for.

A synchronous tool runs in a worker thread so it cannot block the event loop. The guarantee is
precise: **the caller stops waiting on schedule; the work may continue.** A thread cannot be
stopped, so a timed-out call's thread runs to its end, and its side effect may still happen.

What bounds that work:

- The executor keeps the tool's ``max_concurrency`` slot until every thread the call started has
  finished, and never retries a timed-out call over its own still-running thread.
- Every tool thread in the process runs in one pool of ``max_workers`` threads (the Runtime's
  ``runtime.tool_threads`` settings), separate from asyncio's default executor, so stuck tools
  never hold up the event loop's other blocking work. When every worker is busy, up to
  ``max_queued`` calls wait for one; past that a call is refused at once with
  :class:`ToolCapacityError`, before it starts. A queued call whose caller stops waiting is
  withdrawn and never starts.

A tool that needs a hard deadline sets ``ToolDefinition.execution = "process"``: see
:mod:`algen_agent_runtime.tools.process`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from algen_agent_runtime.exceptions.errors import ToolCapacityError

#: Default number of tool worker threads: asyncio's own default for ``to_thread``.
DEFAULT_MAX_WORKERS = min(32, (os.cpu_count() or 1) + 4)
#: Default number of calls that may wait for a busy pool before calls are refused.
DEFAULT_MAX_QUEUED = 128


class ToolThreadPool:
    """A bounded thread pool with a bounded queue in front of it."""

    def __init__(
        self, max_workers: int = DEFAULT_MAX_WORKERS, max_queued: int = DEFAULT_MAX_QUEUED
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if max_queued < 0:
            raise ValueError("max_queued must not be negative")
        self.max_workers = max_workers
        self.max_queued = max_queued
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers, thread_name_prefix="algen-tool"
        )
        self._lock = threading.Lock()
        self._pending = 0

    @property
    def pending(self) -> int:
        """Calls running or waiting for a worker."""
        with self._lock:
            return self._pending

    def submit(
        self, function: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> concurrent.futures.Future[Any]:
        """Start ``function`` in a worker, or queue it; raise ``ToolCapacityError`` when full."""
        with self._lock:
            if self._pending >= self.max_workers + self.max_queued:
                raise ToolCapacityError(self.max_workers, self.max_queued)
            self._pending += 1
        context = contextvars.copy_context()
        try:
            future = self._executor.submit(
                context.run, functools.partial(function, *args, **kwargs)
            )
        except BaseException:
            self._release()
            raise
        future.add_done_callback(lambda _: self._release())
        return future

    def _release(self) -> None:
        with self._lock:
            self._pending -= 1


_pool_lock = threading.Lock()
_pool: ToolThreadPool | None = None


def tool_thread_pool() -> ToolThreadPool:
    """The process's tool thread pool, created with the defaults on first use."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ToolThreadPool()
        return _pool


def configure_tool_threads(
    max_workers: int | None = None, max_queued: int = DEFAULT_MAX_QUEUED
) -> ToolThreadPool:
    """Size the process's tool thread pool (``None`` workers: the default).

    The pool is per process: the last call wins. Calls already running or queued in the previous
    pool finish there.
    """
    global _pool
    workers = max_workers if max_workers is not None else DEFAULT_MAX_WORKERS
    with _pool_lock:
        current = _pool
        if current is not None and (current.max_workers, current.max_queued) == (
            workers,
            max_queued,
        ):
            return current
        _pool = ToolThreadPool(workers, max_queued)
    if current is not None:
        current._executor.shutdown(wait=False)
    return _pool


_threads: ContextVar[list[concurrent.futures.Future[Any]] | None] = ContextVar(
    "algen_agent_runtime_tool_threads", default=None
)


async def run_blocking[T](function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run ``function`` in a tool worker thread and wait for it.

    Raises :class:`ToolCapacityError` at once when the pool and its queue are full. Cancelling
    the wait (a timeout) withdraws a call still queued; one already running keeps running, and
    the tool call that started it is told, through :func:`track_threads`, so it can hold its
    capacity until the thread finishes.
    """
    future = tool_thread_pool().submit(function, *args, **kwargs)
    threads = _threads.get()
    if threads is not None:
        threads.append(future)
    work = asyncio.wrap_future(future)
    work.add_done_callback(_retrieve)
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        future.cancel()  # Only a call that has not started yet can be withdrawn.
        raise


@contextmanager
def track_threads() -> Iterator[list[concurrent.futures.Future[Any]]]:
    """Collect the worker-thread calls :func:`run_blocking` starts within the block."""
    threads: list[concurrent.futures.Future[Any]] = []
    token = _threads.set(threads)
    try:
        yield threads
    finally:
        _threads.reset(token)


def when_finished(threads: list[concurrent.futures.Future[Any]]) -> asyncio.Future[Any] | None:
    """A future done once every call in ``threads`` has finished (or was withdrawn unstarted),
    or None when they all have already."""
    running = [future for future in threads if not future.done()]
    if not running:
        return None
    return asyncio.gather(*(asyncio.wrap_future(f) for f in running), return_exceptions=True)


def _retrieve(work: asyncio.Future[Any]) -> None:
    # A thread nobody waits for any more may still fail; retrieving its error keeps asyncio from
    # logging "exception was never retrieved". The executor already recorded the call's outcome.
    if not work.cancelled():
        work.exception()

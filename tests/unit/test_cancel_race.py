"""Cancelling a working run: the cancel and the drive it stops both end the run (found by the
long soak).

``Runtime.cancel`` reads the run, cancels its drive and saves it cancelled; the drive, on its
cancellation, also saves it cancelled. Whichever saved second hit a version conflict: in the drive
that was an unhandled ``RunConflictError`` ("Task exception was never retrieved" with a traceback
for many cancels under load), and in ``cancel`` a conflict error for a run that was cancelled.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import make_runtime

from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.types.contracts import ModelRequest, ModelResponse, RunRequest, RunStatus


class StuckProvider(MockModelProvider):
    async def generate(self, request: ModelRequest) -> ModelResponse:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class SlowFirstCancelSave:
    """Wraps the run store: the first save of a cancelled run waits, so the other writer saves
    the run cancelled first and this one conflicts."""

    def __init__(self, store: Any, delay: float) -> None:
        self._store = store
        self._delay = delay
        self._delayed = False

    async def save(self, state: Any, expected_version: Any = None) -> Any:
        if state.status is RunStatus.CANCELLED and not self._delayed:
            self._delayed = True
            await asyncio.sleep(self._delay)
        return await self._store.save(state, expected_version)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


async def _working_run() -> tuple[Any, str, asyncio.Task[None]]:
    runtime = make_runtime(StuckProvider())
    state = await runtime.start(
        RunRequest(agent="test-agent", input="hello", tenant_id="t", user_id="u")
    )
    for _ in range(400):
        current = await runtime.status(state.id, "t")
        if current.status is RunStatus.INVOKING_MODEL:
            break
        await asyncio.sleep(0.005)
    assert current.status is RunStatus.INVOKING_MODEL
    return runtime, state.id, runtime._tasks[state.id]


@pytest.mark.parametrize("delay", [0.0, 0.05])
async def test_cancel_and_the_cancelled_drive_end_the_run_once(delay: float) -> None:
    runtime, run_id, drive = await _working_run()
    runtime.runs = SlowFirstCancelSave(runtime.runs, delay)
    try:
        cancelled = await runtime.cancel(run_id, "t")
        await asyncio.wait({drive}, timeout=5)

        assert drive.done()
        assert drive.exception() is None
        assert cancelled.status is RunStatus.CANCELLED
        assert (await runtime.status(run_id, "t")).status is RunStatus.CANCELLED
        events = [event.type for event in await runtime.events.history(run_id)]
        assert events.count("run.cancelled") == 1, events
    finally:
        await runtime.shutdown(grace_seconds=5)

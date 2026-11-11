"""Two processes recovering the same run: the loser stands down (RT-B07, cheap fix).

``recover()`` skips a run another process changed since it was listed, instead of failing the
start, and an event whose sequence another writer took is a ``RunConflictError``, so the
losing drive stops without failing the run. The PostgreSQL race itself is reproduced in
``tests/integration/test_recovery_race_postgres.py``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from conftest import make_runtime

from algen_agent_runtime.events.bus import PostgresEventBus
from algen_agent_runtime.events.contracts import RunEvent
from algen_agent_runtime.exceptions.errors import RunConflictError
from algen_agent_runtime.types.contracts import RunRequest, RunState, RunStatus, utc_now


class UniqueViolationError(Exception):
    """Stands in for ``asyncpg.exceptions.UniqueViolationError`` (matched by name)."""

    def __init__(self, message: str, constraint_name: str) -> None:
        super().__init__(message)
        self.constraint_name = constraint_name


class _Database:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def execute(self, *args: Any) -> None:
        raise self.error


def _event() -> RunEvent:
    return RunEvent(
        type="run.started",
        run_id="run-1",
        tenant_id="t",
        session_id="s",
        correlation_id="c",
        sequence=3,
    )


async def test_a_taken_event_sequence_is_a_run_conflict() -> None:
    bus = PostgresEventBus(
        _Database(
            UniqueViolationError("duplicate key", "algen_agent_runtime_events_run_id_sequence_key")
        )
    )
    with pytest.raises(RunConflictError, match="event 3 of run 'run-1'"):
        await bus.publish(_event())


async def test_other_database_errors_pass_through() -> None:
    primary = PostgresEventBus(
        _Database(UniqueViolationError("duplicate key", "algen_agent_runtime_events_pkey"))
    )
    with pytest.raises(UniqueViolationError):
        await primary.publish(_event())
    broken = PostgresEventBus(_Database(OSError("connection reset")))
    with pytest.raises(OSError):
        await broken.publish(_event())


class _RacingRunStore:
    """Wraps a run store; the first save of ``run_id`` loses to another process."""

    def __init__(self, inner: Any, run_id: str) -> None:
        self._inner = inner
        self._run_id = run_id
        self._raced = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def save(self, state: RunState, expected_version: int) -> RunState:
        if state.id == self._run_id and not self._raced:
            self._raced = True
            raise RunConflictError("run changed")
        return await self._inner.save(state, expected_version)


async def test_recover_skips_a_run_another_process_took_and_recovers_the_rest() -> None:
    runtime = make_runtime()
    states = []
    for name in ("taken", "mine"):
        state = RunState(
            request=RunRequest(agent="test-agent", input=name, tenant_id="t", user_id="u"),
            agent_key="test-agent@1.0.0",
            # Recovery rewinds a run interrupted mid-tool, so it saves before driving it.
            status=RunStatus.INVOKING_TOOL,
            deadline=utc_now() + timedelta(seconds=30),
        )
        await runtime.runs.create(state)
        states.append(state)
    runtime.runs = _RacingRunStore(runtime.runs, states[0].id)  # type: ignore[assignment]

    assert await runtime.recover() == 1

    for _ in range(200):
        mine = await runtime.status(states[1].id, "t")
        if mine.status == RunStatus.COMPLETED:
            break
        await asyncio.sleep(0.005)
    assert mine.status == RunStatus.COMPLETED
    # The skipped run is untouched: the process that took it drives it.
    taken = await runtime.status(states[0].id, "t")
    assert taken.status == RunStatus.INVOKING_TOOL
    await runtime.shutdown(grace_seconds=5)


async def test_a_drive_that_loses_an_event_sequence_stands_down_without_failing() -> None:
    runtime = make_runtime()
    publish = runtime.events.publish
    lost = False

    async def racing_publish(event: RunEvent) -> None:
        nonlocal lost
        if event.type == "model.started" and not lost:
            lost = True
            raise RunConflictError("event taken")
        await publish(event)

    runtime.events.publish = racing_publish  # type: ignore[method-assign]
    result = await runtime.run(
        RunRequest(agent="test-agent", input="hello", tenant_id="t", user_id="u")
    )
    assert lost
    # The drive stood down: the run is neither failed nor marked by this process.
    assert result.status not in {RunStatus.FAILED, RunStatus.COMPLETED}
    assert result.error is None

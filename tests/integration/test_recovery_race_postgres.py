"""Two runtimes recovering the same interrupted run on one PostgreSQL database (RT-B07).

Recovery is single-node until 0.3 brings recovery leases, but a second process that runs it
by mistake must not fail its own start or the run: one drives the run, the other skips it or
stands down, and the run completes.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from conftest import make_runtime

from algen_agent_runtime.events.bus import PostgresEventBus
from algen_agent_runtime.events.contracts import RunEvent
from algen_agent_runtime.exceptions.errors import RunConflictError
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.persistence.postgres import PostgresDatabase, PostgresRunStore
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.types.contracts import (
    TERMINAL_STATUSES,
    ModelRequest,
    ModelResponse,
    RunRequest,
    RunState,
    RunStatus,
    utc_now,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("POSTGRES_DSN"), reason="the recovery race needs a live PostgreSQL"
    ),
]


@pytest.fixture
async def database() -> AsyncIterator[PostgresDatabase]:
    pytest.importorskip("asyncpg")
    db = PostgresDatabase(os.environ["POSTGRES_DSN"])
    await db.start()
    try:
        yield db
    finally:
        await db.close()


class _SlowProvider(MockModelProvider):
    """Answers after a short wait, so two drives of one run overlap."""

    async def generate(self, request: ModelRequest) -> ModelResponse:
        await asyncio.sleep(0.05)
        return await super().generate(request)


class _ListedTogether(PostgresRunStore):
    """Both processes list the active runs before either changes one.

    Only this test's run is listed, so other tests' runs in the shared database are left alone.
    """

    def __init__(self, database: PostgresDatabase, barrier: asyncio.Barrier, run_id: str) -> None:
        super().__init__(database)
        self._barrier = barrier
        self._run_id = run_id

    async def list_active(self, limit: int = 1000) -> tuple[RunState, ...]:
        listed = tuple(
            state for state in await super().list_active(limit) if state.id == self._run_id
        )
        await self._barrier.wait()
        return listed


def _replica(database: PostgresDatabase, barrier: asyncio.Barrier, run_id: str) -> AgentRuntime:
    runtime = make_runtime(_SlowProvider(["answer"] * 4))
    runtime.runs = _ListedTogether(database, barrier, run_id)
    runtime.events = PostgresEventBus(database)
    return runtime


async def _interrupted_run(database: PostgresDatabase, status: RunStatus) -> RunState:
    tenant = f"tenant-{uuid4().hex}"
    state = RunState(
        request=RunRequest(agent="test-agent", input="hello", tenant_id=tenant, user_id="u"),
        agent_key="test-agent@1.0.0",
        status=status,
        deadline=utc_now() + timedelta(seconds=30),
    )
    await PostgresRunStore(database).create(state)
    return state


async def _settled(runtimes: tuple[AgentRuntime, ...], state: RunState) -> RunState:
    """The run once it has ended and neither process is still driving it."""
    for _ in range(400):
        current = await runtimes[0].status(state.id, state.request.tenant_id)
        if current.status in TERMINAL_STATUSES and not any(
            _busy(runtime, state) for runtime in runtimes
        ):
            return current
        await asyncio.sleep(0.025)
    raise AssertionError(f"run did not settle: {current.status}")


def _busy(runtime: AgentRuntime, state: RunState) -> bool:
    task = runtime._tasks.get(state.id)
    return task is not None and not task.done()


@pytest.mark.parametrize(
    "status",
    [
        # Recovery rewinds this checkpoint, so both processes write it: one save loses.
        RunStatus.INVOKING_MODEL,
        # Nothing to rewind, so both processes drive it: one drive loses a save or an event
        # sequence to the other and stands down.
        RunStatus.PLANNING,
    ],
)
async def test_two_runtimes_recovering_one_run_neither_fails_it(
    database: PostgresDatabase, status: RunStatus
) -> None:
    state = await _interrupted_run(database, status)
    barrier = asyncio.Barrier(2)
    first, second = _replica(database, barrier, state.id), _replica(database, barrier, state.id)
    try:
        # Neither start fails.
        recovered = await asyncio.wait_for(
            asyncio.gather(first.recover(), second.recover()), timeout=30
        )
        # A rewound checkpoint is taken by one process; one needing no rewind by both, and
        # the drive that loses stands down.
        assert sum(recovered) == (1 if status == RunStatus.INVOKING_MODEL else 2)
        current = await _settled((first, second), state)
        assert current.status == RunStatus.COMPLETED, current.error
        assert current.error is None
        events = await first.events.history(state.id)
        types = [event.type for event in events]
        assert "run.failed" not in types
        assert types.count("run.completed") == 1
        sequences = [event.sequence for event in events]
        assert sequences == sorted(set(sequences))
    finally:
        await asyncio.gather(first.shutdown(grace_seconds=5), second.shutdown(grace_seconds=5))


async def test_a_recovering_runtime_skips_a_run_another_one_already_took(
    database: PostgresDatabase,
) -> None:
    state = await _interrupted_run(database, RunStatus.INVOKING_MODEL)
    barrier = asyncio.Barrier(2)
    first, second = _replica(database, barrier, state.id), _replica(database, barrier, state.id)
    try:
        results: list[Any] = await asyncio.wait_for(
            asyncio.gather(first.recover(), second.recover(), return_exceptions=True),
            timeout=30,
        )
        # Before the fix the losing process raised RunConflictError out of its start.
        assert not any(isinstance(item, BaseException) for item in results), results
        assert sorted(results) == [0, 1]
        assert (await _settled((first, second), state)).status == RunStatus.COMPLETED
    finally:
        await asyncio.gather(first.shutdown(grace_seconds=5), second.shutdown(grace_seconds=5))


async def test_an_event_sequence_another_writer_took_is_a_run_conflict(
    database: PostgresDatabase,
) -> None:
    state = await _interrupted_run(database, RunStatus.PLANNING)
    bus = PostgresEventBus(database)
    sequence = await bus.next_sequence(state.id)

    def event() -> RunEvent:
        return RunEvent(
            type="step.started",
            run_id=state.id,
            tenant_id=state.request.tenant_id,
            session_id=state.session_id,
            correlation_id=state.request.correlation_id,
            sequence=sequence,
        )

    await bus.publish(event())
    # The losing drive gets RunConflictError, which it treats as "superseded", not the raw
    # asyncpg UniqueViolationError that failed the run before.
    with pytest.raises(RunConflictError, match=f"event {sequence} of run"):
        await bus.publish(event())
    assert [item.sequence for item in await bus.history(state.id)] == [sequence]

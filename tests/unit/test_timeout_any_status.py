"""A run whose deadline passes in any working status ends timed_out (found by the long soak).

Under load a run's deadline can pass while it plans, verifies or composes (waiting for a
database connection, say). The state machine allowed ``timed_out`` only from some statuses, so
the drive's timeout raised ``ConflictError: invalid run transition: planning -> timed_out``
("Task exception was never retrieved") and the run was left in its working status.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.planning.planners import DirectPlanner
from algen_agent_runtime.runtime.state_machine import validate_transition
from algen_agent_runtime.types.contracts import (
    PAUSED_STATUSES,
    TERMINAL_STATUSES,
    RunRequest,
    RunStatus,
)


@pytest.mark.parametrize(
    "status", [status for status in RunStatus if status not in TERMINAL_STATUSES]
)
def test_every_unfinished_status_may_time_out(status: RunStatus) -> None:
    validate_transition(status, RunStatus.TIMED_OUT)


class SlowPlanner(DirectPlanner):
    name = "slow"

    async def plan(self, state, agent):  # type: ignore[no-untyped-def]
        await asyncio.sleep(5)
        return await super().plan(state, agent)


async def test_a_run_timing_out_while_it_plans_ends_timed_out() -> None:
    runtime = make_runtime(MockModelProvider(["answer"]), make_agent(planning_strategy="slow"))
    runtime.planners.register(SlowPlanner())
    try:
        state = await runtime.start(
            RunRequest(
                agent="test-agent", input="hi", tenant_id="t", user_id="u", timeout_seconds=0.2
            )
        )
        done = await asyncio.wait_for(runtime.wait(state.id, "t"), timeout=5)
        assert done.status is RunStatus.TIMED_OUT
        assert done.status not in PAUSED_STATUSES
    finally:
        await runtime.shutdown(grace_seconds=5)

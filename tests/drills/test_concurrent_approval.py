"""Concurrent approval drill: two Runtime processes on one database decide the same approval."""

from __future__ import annotations

import asyncio

import pytest
from drill_support import (
    APPROVAL_AGENT,
    HEADERS,
    TENANT,
    TOOL,
    USER,
    fresh_database,
    ledger,
    requires_postgres,
    running,
    side_effects,
    until,
)

from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.types.contracts import RunRequest, RunStatus

pytestmark = [pytest.mark.drill, pytest.mark.integration, pytest.mark.postgres, requires_postgres]

RACES = 10


async def _paused(process: object, text: str) -> str:
    runtime = process.runtime  # type: ignore[attr-defined]
    state = await runtime.start(
        RunRequest(agent=APPROVAL_AGENT, input=text, tenant_id=TENANT, user_id=USER)
    )

    async def waiting() -> bool:
        current = await runtime.status(state.id, TENANT)
        return current.status is RunStatus.AWAITING_APPROVAL

    await until(waiting, "the approval pause")
    return str(state.id)


async def test_two_processes_deciding_one_approval_resume_the_run_once() -> None:
    async with fresh_database() as dsn, running(dsn) as one, running(dsn) as two:
        for race in range(RACES):
            run_id = await _paused(one, f"race {race}")
            decisions = ("approved", "rejected") if race % 2 else ("approved", "approved")
            outcomes = await asyncio.gather(
                one.runtime.resume(run_id, TENANT, {"decision": decisions[0]}),
                two.runtime.resume(run_id, TENANT, {"decision": decisions[1]}),
                return_exceptions=True,
            )
            winners = [item for item in outcomes if not isinstance(item, BaseException)]
            losers = [item for item in outcomes if isinstance(item, BaseException)]
            assert len(winners) == 1 and len(losers) == 1, outcomes
            assert isinstance(losers[0], ConflictError), repr(losers[0])
            winner = 0 if outcomes[0] is winners[0] else 1
            process = (one, two)[winner]
            result = await process.runtime.wait(run_id, TENANT)
            if decisions[winner] == "approved":
                assert result.status is RunStatus.COMPLETED, result.error
                assert len(await side_effects(dsn, run_id)) == 1
                assert await ledger(dsn, run_id) == [(TOOL, "completed")]
            else:
                assert result.status is RunStatus.FAILED
                assert await side_effects(dsn, run_id) == []
                assert await ledger(dsn, run_id) == []
            # Neither process drives it again.
            for other in (one, two):
                assert run_id not in other.runtime._tasks
            history = [event.type for event in await one.runtime.events.history(run_id)]
            assert history.count("run.completed") + history.count("run.failed") == 1


async def test_two_processes_deciding_through_the_api_one_gets_409() -> None:
    async with fresh_database() as dsn, running(dsn) as one, running(dsn) as two:
        run_id = await _paused(one, "through the api")
        responses = await asyncio.gather(
            *(
                process.client.post(
                    f"/v1/runs/{run_id}/approval", json={"decision": "approved"}, headers=HEADERS
                )
                for process in (one, two)
            )
        )
        assert sorted(response.status_code for response in responses) == [202, 409]
        winner = (one, two)[[response.status_code for response in responses].index(202)]
        result = await winner.runtime.wait(run_id, TENANT)
        assert result.status is RunStatus.COMPLETED, result.error
        assert len(await side_effects(dsn, run_id)) == 1

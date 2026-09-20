import asyncio

from conftest import make_runtime

from algen_agent_runtime.types.contracts import RunRequest, RunStatus


async def test_concurrent_runs_remain_isolated() -> None:
    runtime = make_runtime()
    results = await asyncio.gather(
        *(
            runtime.run(
                RunRequest(
                    agent="test-agent",
                    input=f"request-{index}",
                    tenant_id=f"tenant-{index % 3}",
                    user_id=f"user-{index}",
                )
            )
            for index in range(30)
        )
    )
    assert all(result.status == RunStatus.COMPLETED for result in results)
    assert len({result.run_id for result in results}) == 30

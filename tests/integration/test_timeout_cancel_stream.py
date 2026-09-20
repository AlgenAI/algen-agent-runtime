import asyncio

from conftest import make_runtime

from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.types.contracts import RunRequest, RunStatus


async def test_timeout_is_terminal() -> None:
    runtime = make_runtime(MockModelProvider(["late"], delay_seconds=0.1))
    result = await runtime.run(
        RunRequest(agent="test-agent", input="x", tenant_id="t", user_id="u", timeout_seconds=0.01)
    )
    assert result.status == RunStatus.TIMED_OUT


async def test_cancellation_is_terminal() -> None:
    runtime = make_runtime(MockModelProvider(["late"], delay_seconds=1))
    state = await runtime.start(
        RunRequest(agent="test-agent", input="x", tenant_id="t", user_id="u")
    )
    await asyncio.sleep(0)
    cancelled = await runtime.cancel(state.id, "t")
    assert cancelled.status == RunStatus.CANCELLED


async def test_streaming_emits_deltas() -> None:
    runtime = make_runtime(MockModelProvider(["one two"]))
    result = await runtime.run(
        RunRequest(agent="test-agent", input="x", tenant_id="t", user_id="u", stream=True)
    )
    history = await runtime.events.history(result.run_id)
    assert any(event.type == "model.delta" for event in history)
    assert history[-1].type == "run.completed"

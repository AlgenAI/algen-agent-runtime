import pytest

from algen_agent_runtime.exceptions.errors import ToolExecutionError
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.contracts import Tool, ToolContext, ToolDefinition
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.types.contracts import RetryPolicy


def context() -> ToolContext:
    return ToolContext(
        run_id="r",
        step_id="s",
        tenant_id="t",
        user_id="u",
        permissions=frozenset(),
        idempotency_key="key",
    )


async def test_tool_input_output_and_idempotency_contract() -> None:
    calls = 0

    async def handler(arguments, tool_context):
        nonlocal calls
        calls += 1
        return {"value": arguments["value"] + 1}

    registry = ToolRegistry()
    registry.register(
        Tool(
            ToolDefinition(
                name="test.increment",
                version="1.0.0",
                description="increment",
                input_schema={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                },
                output_schema={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                },
            ),
            handler,
        )
    )
    executor = ToolExecutor(registry, CompositePolicyEngine())
    assert (await executor.execute("test.increment", {"value": 1}, context())).value == {"value": 2}
    assert (await executor.execute("test.increment", {"value": 1}, context())).value == {"value": 2}
    assert calls == 1


async def test_invalid_tool_input_is_bounded_error() -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(
            ToolDefinition(
                name="test.input",
                version="1",
                description="test",
                input_schema={"type": "object", "required": ["x"]},
                output_schema={},
            ),
            lambda arguments, tool_context: {},
        )
    )
    with pytest.raises(ToolExecutionError, match="invalid input"):
        await ToolExecutor(registry, CompositePolicyEngine()).execute("test.input", {}, context())


async def test_idempotent_tool_retries_transient_handler_failure() -> None:
    calls = 0

    async def flaky(arguments, tool_context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("transient")
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        Tool(
            ToolDefinition(
                name="test.flaky",
                version="1",
                description="flaky",
                input_schema={"type": "object"},
                output_schema={"type": "object", "required": ["ok"]},
                retry_policy=RetryPolicy(max_attempts=2, initial_backoff_seconds=0),
            ),
            flaky,
        )
    )
    result = await ToolExecutor(registry, CompositePolicyEngine()).execute(
        "test.flaky", {}, context()
    )
    assert result.value == {"ok": True}
    assert calls == 2

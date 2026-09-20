import asyncio

from conftest import make_agent, make_runtime

from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolDefinition
from algen_agent_runtime.types.contracts import RunRequest, RunStatus


async def test_model_tool_model_loop() -> None:
    runtime = make_runtime(
        MockModelProvider([tool_call_response("math.add", {"a": 2, "b": 3}), "five"]),
        make_agent(enabled_tools=frozenset({"math.add"})),
    )
    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="math.add",
                version="1",
                description="add",
                input_schema={
                    "type": "object",
                    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                    "required": ["a", "b"],
                },
                output_schema={
                    "type": "object",
                    "properties": {"sum": {"type": "integer"}},
                    "required": ["sum"],
                },
            ),
            lambda args, ctx: {"sum": args["a"] + args["b"]},
        )
    )
    result = await runtime.run(
        RunRequest(agent="test-agent", input="2+3", tenant_id="t", user_id="u")
    )
    assert result.status == RunStatus.COMPLETED
    assert result.output == "five"
    assert result.execution_summary.tool_calls == 1


async def test_side_effect_approval_pause_resume() -> None:
    runtime = make_runtime(
        MockModelProvider([tool_call_response("external.write", {"value": "x"}), "done"]),
        make_agent(
            enabled_tools=frozenset({"external.write"}), tool_permissions=frozenset({"write"})
        ),
    )
    calls = 0

    async def handler(args, ctx):
        nonlocal calls
        calls += 1
        return {"ok": True}

    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="external.write",
                version="1",
                description="write",
                required_permissions=frozenset({"write"}),
                side_effect=SideEffect.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
                output_schema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
            ),
            handler,
        )
    )
    paused = await runtime.run(
        RunRequest(agent="test-agent", input="write", tenant_id="t", user_id="u")
    )
    assert paused.status == RunStatus.AWAITING_APPROVAL
    assert calls == 0
    await runtime.resume(paused.run_id, "t", {"decision": "approved"})
    for _ in range(100):
        state = await runtime.status(paused.run_id, "t")
        if state.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
            break
        await asyncio.sleep(0.001)
    assert state.status == RunStatus.COMPLETED
    assert calls == 1

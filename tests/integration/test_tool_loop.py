import asyncio

from conftest import make_agent, make_runtime

from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolDefinition
from algen_agent_runtime.types.contracts import (
    ContinuationPolicy,
    FinishReason,
    Message,
    ModelResponse,
    Role,
    RunRequest,
    RunStatus,
    ToolCall,
)


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


async def test_direct_planner_completes_every_parallel_tool_call_before_model_continues() -> None:
    calls = tuple(
        ToolCall(id=f"call-{index}", name="records.read", arguments={"id": index})
        for index in range(1, 4)
    )
    provider = MockModelProvider(
        [
            ModelResponse(
                message=Message.text(Role.ASSISTANT, ""),
                tool_calls=calls,
                finish_reason=FinishReason.TOOL_CALLS,
                model="deterministic",
                provider="mock",
            ),
            "summary ready",
        ]
    )
    runtime = make_runtime(
        provider,
        make_agent(
            planning_strategy="direct",
            enabled_tools=frozenset({"records.read"}),
        ),
    )
    executed: list[int] = []

    async def handler(args, ctx):
        executed.append(args["id"])
        return {"id": args["id"], "found": True}

    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="records.read",
                version="1",
                description="read a record",
                input_schema={
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "found": {"type": "boolean"},
                    },
                    "required": ["id", "found"],
                },
            ),
            handler,
        )
    )

    result = await runtime.run(
        RunRequest(agent="test-agent", input="summarize", tenant_id="t", user_id="u")
    )

    assert result.status == RunStatus.COMPLETED
    assert result.output == "summary ready"
    assert executed == [1, 2, 3]
    assert result.execution_summary.tool_calls == 3
    assert len(provider.requests) == 2
    follow_up = provider.requests[1]
    assert sum(message.role == Role.SYSTEM for message in follow_up.messages) == 1
    assert {message.tool_call_id for message in follow_up.messages if message.role == Role.TOOL} == {
        call.id for call in calls
    }


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


async def test_execution_budget_continues_automatically_without_losing_progress() -> None:
    runtime = make_runtime(
        MockModelProvider([tool_call_response("records.read", {"id": 7}), "ready"]),
        make_agent(
            enabled_tools=frozenset({"records.read"}),
            max_steps=3,
            continuation_policy=ContinuationPolicy(
                enabled=True,
                automatic_extensions=1,
                step_increment=2,
            ),
        ),
    )
    calls = 0

    async def handler(args, ctx):
        nonlocal calls
        calls += 1
        return {"id": args["id"], "found": True}

    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="records.read",
                version="1",
                description="read",
                input_schema={
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "found": {"type": "boolean"},
                    },
                    "required": ["id", "found"],
                },
            ),
            handler,
        )
    )

    result = await runtime.run(
        RunRequest(agent="test-agent", input="read it", tenant_id="t", user_id="u")
    )
    state = await runtime.status(result.run_id, "t")
    history = await runtime.events.history(result.run_id)

    assert result.status == RunStatus.COMPLETED
    assert result.output == "ready"
    assert calls == 1
    assert state.continuation_count == 1
    assert any(
        event.type == "run.continued" and event.data["mode"] == "automatic"
        for event in history
    )


async def test_execution_budget_pauses_then_resumes_after_approval() -> None:
    runtime = make_runtime(
        MockModelProvider([tool_call_response("records.read", {"id": 9}), "finished"]),
        make_agent(
            enabled_tools=frozenset({"records.read"}),
            max_steps=2,
            continuation_policy=ContinuationPolicy(
                enabled=True,
                automatic_extensions=0,
                step_increment=2,
            ),
        ),
    )

    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="records.read",
                version="1",
                description="read",
                input_schema={
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "found": {"type": "boolean"},
                    },
                    "required": ["id", "found"],
                },
            ),
            lambda args, ctx: {"id": args["id"], "found": True},
        )
    )

    paused = await runtime.run(
        RunRequest(agent="test-agent", input="read it", tenant_id="t", user_id="u")
    )
    assert paused.status == RunStatus.AWAITING_APPROVAL
    state = await runtime.status(paused.run_id, "t")
    assert state.pause_payload is not None
    assert state.pause_payload["kind"] == "execution_continuation"
    assert state.pause_payload["reason"] == "execution_steps"

    await runtime.resume(paused.run_id, "t", {"decision": "approved"})
    result = await runtime.wait(paused.run_id, "t")

    assert result.status == RunStatus.COMPLETED
    assert result.output == "finished"
    assert (await runtime.status(paused.run_id, "t")).continuation_count == 1


async def test_execution_budget_rejection_stops_without_replaying_work() -> None:
    runtime = make_runtime(
        MockModelProvider([tool_call_response("records.read", {"id": 11})]),
        make_agent(
            enabled_tools=frozenset({"records.read"}),
            max_steps=2,
            continuation_policy=ContinuationPolicy(enabled=True, step_increment=2),
        ),
    )
    calls = 0

    async def handler(args, ctx):
        nonlocal calls
        calls += 1
        return {"id": args["id"], "found": True}

    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="records.read",
                version="1",
                description="read",
                input_schema={
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "found": {"type": "boolean"},
                    },
                    "required": ["id", "found"],
                },
            ),
            handler,
        )
    )

    paused = await runtime.run(
        RunRequest(agent="test-agent", input="read it", tenant_id="t", user_id="u")
    )
    assert paused.status == RunStatus.AWAITING_APPROVAL
    assert calls == 1

    rejected = await runtime.resume(paused.run_id, "t", {"decision": "rejected"})

    assert rejected.status == RunStatus.FAILED
    assert calls == 1

"""``budget.max_cost_usd: 0`` lets a zero-cost model run and stops at any spend (#83 item 9)."""

from __future__ import annotations

from typing import Any

from conftest import make_agent, make_runtime

from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.tools.contracts import Tool, ToolDefinition
from algen_agent_runtime.types.contracts import (
    Budget,
    FinishReason,
    Message,
    ModelResponse,
    Role,
    RunRequest,
    RunStatus,
    TokenUsage,
    ToolCall,
)


def _request() -> RunRequest:
    return RunRequest(agent="test-agent", input="go", tenant_id="t", user_id="u")


def _tool_call() -> ModelResponse:
    return ModelResponse(
        message=Message.text(Role.ASSISTANT, ""),
        tool_calls=(ToolCall(name="clock.now", arguments={}),),
        finish_reason=FinishReason.TOOL_CALLS,
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        model="deterministic",
        provider="mock",
    )


def _runtime(cost_per_1k: float) -> tuple[Any, list[object]]:
    calls: list[object] = []
    router = ModelRouter(circuit_failure_threshold=1)
    router.register_provider(
        MockModelProvider([_tool_call(), "done"]),
        cost_per_1k_input=cost_per_1k,
        cost_per_1k_output=cost_per_1k,
    )
    runtime = make_runtime(
        agent=make_agent(budget=Budget(max_cost_usd=0), enabled_tools=frozenset({"clock.now"}))
    )
    runtime.router = router
    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="clock.now",
                version="1",
                description="The time",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
            ),
            lambda args, ctx: calls.append(args) or {"now": "noon"},
        )
    )
    return runtime, calls


async def test_a_zero_cost_model_runs_under_a_zero_budget() -> None:
    runtime, calls = _runtime(0.0)

    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED
    assert len(calls) == 1


async def test_any_spend_stops_a_zero_budget() -> None:
    runtime, _calls = _runtime(1.0)

    result = await runtime.run(_request())

    assert result.status != RunStatus.COMPLETED
    assert "estimated_cost" in (result.error or "")

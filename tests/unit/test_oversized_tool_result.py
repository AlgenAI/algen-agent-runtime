"""One tool result too large for the context is offloaded or cut, as a last resort."""

from __future__ import annotations

import json
from typing import Any

from conftest import make_agent, make_runtime

from algen_agent_runtime.context.artifacts import (
    CONTEXT_READ_PERMISSION,
    CONTEXT_READ_TOOL,
    ContextArtifactService,
)
from algen_agent_runtime.context.packing import fit_newest_tool_results
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import (
    ResultCoverage,
    SideEffect,
    Tool,
    ToolDefinition,
)
from algen_agent_runtime.types.contracts import (
    ContextPolicy,
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    RunRequest,
    RunStatus,
    ToolCall,
)

# Far past the default builder's 16,000-token context.
HUGE = "row " * 60_000


def _dump(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    return ResultCoverage(searched="orders", returned=60_000, source="system_of_record").attach(
        {"rows": HUGE}
    )


def _provider() -> MockModelProvider:
    def handler(request: ModelRequest) -> ModelResponse:
        if any(message.role == Role.TOOL for message in request.messages):
            return ModelResponse(
                message=Message.text(Role.ASSISTANT, "Here is what fit."),
                finish_reason=FinishReason.STOP,
                model="deterministic",
                provider="mock",
            )
        return tool_call_response("data.dump", {})

    return MockModelProvider(handler=handler)


def _runtime(
    provider: MockModelProvider, *, artifacts: bool = False, tool: Any = _dump, **policy: Any
) -> AgentRuntime:
    tools = {"data.dump"}
    permissions: set[str] = set()
    if artifacts:
        tools.add(CONTEXT_READ_TOOL)
        permissions.add(CONTEXT_READ_PERMISSION)
    agent = make_agent(
        enabled_tools=frozenset(tools),
        tool_permissions=frozenset(permissions),
        context_policy=ContextPolicy(**policy),
    )
    runtime = make_runtime(
        provider,
        agent,
        context_artifacts=ContextArtifactService(InMemoryArtifactStore()) if artifacts else None,
    )
    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="data.dump",
                version="1",
                description="dump every row",
                side_effect=SideEffect.READ,
                input_schema={"type": "object"},
                output_schema={"type": "object"},
            ),
            tool,
        )
    )
    return runtime


def _request() -> RunRequest:
    return RunRequest(agent="test-agent", input="list the orders", tenant_id="t", user_id="u")


def _tool_messages(request: ModelRequest) -> list[Message]:
    return [message for message in request.messages if message.role == Role.TOOL]


async def test_an_oversized_result_is_cut_and_marked_instead_of_failing_the_run() -> None:
    provider = _provider()
    runtime = _runtime(provider)
    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED, result.error
    [message] = _tool_messages(provider.requests[-1])
    value = json.loads(message.text_content)
    assert value["truncated"] is True
    assert value["total_chars"] > value["kept_chars"] > 1_000
    assert "incomplete" in value["notice"]
    # What the tool covered stays visible, marked capped, so absence is not inferred.
    coverage = ResultCoverage.of(value)
    assert coverage is not None and coverage.capped and not coverage.complete
    events = await runtime.events.history(result.run_id)
    [truncated] = [event.data for event in events if event.type == "context.tool_result_truncated"]
    assert truncated["tool"] == "data.dump"
    assert truncated["offloaded"] is False
    assert truncated["kept_chars"] == value["kept_chars"]
    # The run's history keeps the cut result, so later builds do not cut it again.
    state = await runtime.status(result.run_id, "t")
    assert state.context_history is not None
    [stored] = [m for m in state.context_history if m.role == Role.TOOL]
    assert json.loads(stored.text_content)["truncated"] is True
    audits = [
        a for a in await runtime.audits.list("t") if a.action == "context.tool_result_truncated"
    ]
    assert [audit.outcome for audit in audits] == ["truncated"]


async def test_an_oversized_result_is_offloaded_when_the_agent_can_read_it_back() -> None:
    provider = _provider()
    runtime = _runtime(provider, artifacts=True)
    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED, result.error
    [message] = _tool_messages(provider.requests[-1])
    value = json.loads(message.text_content)
    assert value["offloaded"] is True
    assert value["read_tool"] == CONTEXT_READ_TOOL
    assert value["artifact_id"] in result.artifacts
    events = await runtime.events.history(result.run_id)
    [truncated] = [event.data for event in events if event.type == "context.tool_result_truncated"]
    assert truncated["offloaded"] is True
    assert truncated["artifact_id"] == value["artifact_id"]


async def test_fail_keeps_the_earlier_behaviour() -> None:
    runtime = _runtime(_provider(), oversized_tool_result="fail")
    result = await runtime.run(_request())
    assert result.status == RunStatus.FAILED
    assert "required context exceeds" in (result.error or "")
    assert result.error_type == "BudgetExceededError"


async def test_a_result_that_fits_is_left_alone() -> None:
    provider = MockModelProvider(
        [tool_call_response("data.dump", {}), "done"],
    )
    runtime = _runtime(provider, tool=lambda arguments, context: {"rows": "a few"})
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED
    events = await runtime.events.history(result.run_id)
    assert not [event for event in events if event.type == "context.tool_result_truncated"]


def test_fit_gives_up_when_even_empty_results_do_not_fit() -> None:
    call = ToolCall(id="c", name="data.dump", arguments={})
    messages = [
        Message.text(Role.SYSTEM, "s" * 4_000),
        Message.text(Role.USER, "q"),
        Message(role=Role.ASSISTANT, content=(), tool_calls=(call,)),
        Message.text(Role.TOOL, HUGE).model_copy(update={"tool_call_id": "c", "name": "data.dump"}),
    ]
    assert fit_newest_tool_results(messages, 500) is None
    fitted = fit_newest_tool_results(messages, 3_000)
    assert fitted is not None
    [(_message, report)] = fitted
    assert report.original_chars == len(HUGE)
    assert report.kept_chars is not None and 0 < report.kept_chars < len(HUGE)


def test_fit_does_not_apply_without_a_newest_tool_exchange() -> None:
    messages = [Message.text(Role.SYSTEM, "s"), Message.text(Role.USER, HUGE)]
    assert fit_newest_tool_results(messages, 100) is None

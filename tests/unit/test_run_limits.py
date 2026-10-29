"""Per-run limits on model calls, model response size and recorded events."""

from __future__ import annotations

import itertools
from typing import Any

from conftest import make_agent, make_runtime

from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.runtime.guards import wrap_up_note
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolDefinition
from algen_agent_runtime.types.contracts import (
    Budget,
    ContinuationPolicy,
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    RunRequest,
    RunStatus,
    TokenUsage,
    ToolCall,
    WrapUpPolicy,
)

_IDS = itertools.count()


def _lookup() -> Tool:
    return Tool(
        ToolDefinition(
            name="data.lookup",
            version="1",
            description="lookup",
            side_effect=SideEffect.READ,
            volatile=True,
            input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
            output_schema={"type": "object"},
        ),
        lambda arguments, context: {"ok": True},
    )


def _call(arguments: dict[str, Any] | None = None) -> ModelResponse:
    return ModelResponse(
        message=Message.text(Role.ASSISTANT, ""),
        tool_calls=(
            ToolCall(id=f"call-{next(_IDS)}", name="data.lookup", arguments=arguments or {}),
        ),
        finish_reason=FinishReason.TOOL_CALLS,
        usage=TokenUsage(input_tokens=5, output_tokens=5),
        model="deterministic",
        provider="mock",
    )


def _looping_provider() -> MockModelProvider:
    """A model that calls a tool forever."""

    def handler(request: ModelRequest) -> ModelResponse:
        return _call({"key": str(next(_IDS))})

    return MockModelProvider(handler=handler)


def _runtime(provider: MockModelProvider, **agent: Any) -> AgentRuntime:
    agent.setdefault("max_steps", 200)
    runtime = make_runtime(provider, make_agent(enabled_tools=frozenset({"data.lookup"}), **agent))
    runtime.tools.register(_lookup())
    return runtime


def _request(stream: bool = False) -> RunRequest:
    return RunRequest(agent="test-agent", input="go", tenant_id="t", user_id="u", stream=stream)


async def _failure(runtime: AgentRuntime, run_id: str) -> dict[str, Any]:
    events = await runtime.events.history(run_id)
    assert events[-1].type == "run.failed"
    return events[-1].data


async def _limit_audits(runtime: AgentRuntime) -> list[dict[str, Any]]:
    return [
        audit.metadata
        for audit in await runtime.audits.list("t")
        if audit.action == "run.limit_exceeded"
    ]


def test_defaults_are_generous() -> None:
    budget = Budget()
    assert budget.max_model_calls == 1_000
    assert budget.max_response_chars == 1_000_000
    assert budget.max_events == 100_000


async def test_model_call_limit_fails_the_run_with_a_reason() -> None:
    runtime = _runtime(_looping_provider(), budget=Budget(max_model_calls=3))
    result = await runtime.run(_request())

    assert result.status == RunStatus.FAILED
    assert result.execution_summary.model_calls == 3
    assert result.error == "model_calls budget exhausted"
    failed = await _failure(runtime, result.run_id)
    assert failed["reason"] == "limit_exceeded"
    assert failed["limit"] == "model_calls"
    [audit] = await _limit_audits(runtime)
    assert audit["limit"] == "model_calls"
    assert audit["model_calls"] == 3


async def test_model_call_limit_is_extended_by_a_continuation() -> None:
    runtime = _runtime(
        _looping_provider(),
        budget=Budget(max_model_calls=2),
        continuation_policy=ContinuationPolicy(
            enabled=True, automatic_extensions=1, step_increment=2
        ),
    )
    result = await runtime.run(_request())

    assert result.status == RunStatus.AWAITING_APPROVAL
    assert result.execution_summary.model_calls == 4
    events = await runtime.events.history(result.run_id)
    continued = [event.data for event in events if event.type == "run.continued"]
    assert continued == [{"mode": "automatic", "reason": "model_calls"}]
    state = await runtime.status(result.run_id, "t")
    assert state.pause_payload is not None
    assert state.pause_payload["reason"] == "model_calls"


async def test_wrap_up_notice_counts_the_model_calls_left() -> None:
    provider = MockModelProvider([_call(), _call(), "done"])
    runtime = _runtime(
        provider, budget=Budget(max_model_calls=4), wrap_up_policy=WrapUpPolicy(enabled=True)
    )
    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED
    events = await runtime.events.history(result.run_id)
    notices = [event.data for event in events if event.type == "tool.wrap_up"]
    # After 1 and 2 model calls of 4: 2 and 1 more tool calls before the answer.
    assert notices == [
        {"tool_calls_left": 2, "reason": "model_calls"},
        {"tool_calls_left": 1, "reason": "model_calls"},
    ]
    tool_messages = [m for m in provider.requests[-1].messages if m.role == Role.TOOL]
    assert tool_messages[-1].content[1].text == wrap_up_note(1)  # type: ignore[union-attr]


async def test_oversized_response_fails_the_run_and_is_charged() -> None:
    runtime = _runtime(MockModelProvider(["x" * 1_500]), budget=Budget(max_response_chars=1_000))
    result = await runtime.run(_request())

    assert result.status == RunStatus.FAILED
    assert "exceeds the 1000 character limit" in (result.error or "")
    assert (await _failure(runtime, result.run_id))["limit"] == "response_chars"
    assert result.execution_summary.model_calls == 1
    assert result.execution_summary.usage.output_tokens > 0


async def test_oversized_tool_call_arguments_count_toward_the_response() -> None:
    provider = MockModelProvider([_call({"key": "y" * 1_200})])
    runtime = _runtime(provider, budget=Budget(max_response_chars=1_000))
    result = await runtime.run(_request())

    assert result.status == RunStatus.FAILED
    assert (await _failure(runtime, result.run_id))["limit"] == "response_chars"


async def test_streamed_response_stops_as_soon_as_it_is_too_long() -> None:
    words = " ".join(["word"] * 400)
    runtime = _runtime(MockModelProvider([words]), budget=Budget(max_response_chars=1_000))
    result = await runtime.run(_request(stream=True))

    assert result.status == RunStatus.FAILED
    assert (await _failure(runtime, result.run_id))["limit"] == "response_chars"
    streamed = "".join(
        event.data["delta"]
        for event in await runtime.events.history(result.run_id)
        if event.type == "model.delta"
    )
    assert len(streamed) <= 1_000


async def test_event_limit_drops_further_events_and_fails_the_run() -> None:
    runtime = _runtime(_looping_provider(), budget=Budget(max_events=100))
    result = await runtime.run(_request())

    assert result.status == RunStatus.FAILED
    events = await runtime.events.history(result.run_id)
    assert len(events) == 101
    assert [event.sequence for event in events] == list(range(1, 102))
    assert events[-1].type == "run.failed"
    assert events[-1].data["limit"] == "events"
    assert result.error == "run reached its limit of 100 events"
    [audit] = await _limit_audits(runtime)
    assert audit["limit"] == "events"


async def test_runs_within_the_event_limit_are_unaffected() -> None:
    runtime = _runtime(MockModelProvider(["fine"]), budget=Budget(max_events=100))
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED
    # The next run starts its own count.
    assert (await runtime.run(_request())).status == RunStatus.COMPLETED


async def test_streamed_chunks_within_the_interval_share_one_event() -> None:
    runtime = _runtime(MockModelProvider(["one two three four"]))
    runtime.stream_delta_interval_seconds = 60
    result = await runtime.run(_request(stream=True))

    deltas = [
        event.data["delta"]
        for event in await runtime.events.history(result.run_id)
        if event.type == "model.delta"
    ]
    # The first chunk goes out at once; the rest are joined until the stream ends.
    assert deltas == ["one ", "two three four "]
    assert result.output == "one two three four"


async def test_every_chunk_is_an_event_without_an_interval() -> None:
    runtime = _runtime(MockModelProvider(["one two three"]))
    result = await runtime.run(_request(stream=True))
    deltas = [
        event.data["delta"]
        for event in await runtime.events.history(result.run_id)
        if event.type == "model.delta"
    ]
    assert deltas == ["one ", "two ", "three "]

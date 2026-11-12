"""budget.on_limit: summarize. A run at a limit answers once, with no tools, and is partial."""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any

from conftest import make_agent, make_runtime

from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.runtime.guards import (
    LIMIT_ANSWER_NAME,
    LIMIT_TOOL_RESULT_NAME,
    limit_answer_messages,
)
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
    RunResult,
    RunStatus,
    TokenUsage,
    ToolCall,
    VerificationPolicy,
)
from algen_agent_runtime.verification.verifiers import VerificationResult

_IDS = itertools.count()


def _lookup(delay: float = 0) -> Tool:
    async def lookup(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
        if delay:
            await asyncio.sleep(delay)
        return {"value": f"fact-{arguments.get('key')}"}

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
        lookup,
    )


def _call() -> ModelResponse:
    return ModelResponse(
        message=Message.text(Role.ASSISTANT, ""),
        tool_calls=(ToolCall(id=f"call-{next(_IDS)}", name="data.lookup", arguments={"key": "k"}),),
        finish_reason=FinishReason.TOOL_CALLS,
        usage=TokenUsage(input_tokens=5, output_tokens=5),
        model="deterministic",
        provider="mock",
    )


def _provider(answer: str = "Partial: fact-k is all I found.") -> MockModelProvider:
    """A model that calls a tool whenever it may, and answers when it has no tools."""

    def handler(request: ModelRequest) -> ModelResponse:
        if request.tools:
            return _call()
        return ModelResponse(
            message=Message.text(Role.ASSISTANT, answer),
            finish_reason=FinishReason.STOP,
            usage=TokenUsage(input_tokens=7, output_tokens=3),
            model="deterministic",
            provider="mock",
        )

    return MockModelProvider(handler=handler)


def _runtime(provider: MockModelProvider, *, delay: float = 0, **agent: Any) -> AgentRuntime:
    agent.setdefault("max_steps", 200)
    runtime = make_runtime(provider, make_agent(enabled_tools=frozenset({"data.lookup"}), **agent))
    runtime.tools.register(_lookup(delay))
    return runtime


def _request() -> RunRequest:
    return RunRequest(agent="test-agent", input="find the facts", tenant_id="t", user_id="u")


async def test_default_still_fails_at_the_limit_and_says_why() -> None:
    runtime = _runtime(_provider(), budget=Budget(max_model_calls=3))
    result = await runtime.run(_request())

    assert result.status == RunStatus.FAILED
    assert result.output is None
    assert result.partial is False
    assert result.error_type == "BudgetExceededError"
    assert result.limit_reached == "model_calls"


async def test_summarize_answers_once_without_tools_at_the_step_limit() -> None:
    provider = _provider()
    runtime = _runtime(
        provider,
        max_steps=4,
        budget=Budget(on_limit="summarize", limit_answer_max_output_tokens=64),
    )
    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED
    assert result.output == "Partial: fact-k is all I found."
    assert result.partial is True
    assert result.limit_reached == "execution_steps"
    assert result.error_type is None
    final = provider.requests[-1]
    assert final.tools == ()
    assert final.max_output_tokens == 64
    # Tool exchanges are rewritten as text: providers reject tool messages without tools.
    assert all(message.role != Role.TOOL and not message.tool_calls for message in final.messages)
    assert final.messages[-1].name == LIMIT_ANSWER_NAME
    assert "execution steps limit" in final.messages[-1].text_content
    assert any(
        message.name == LIMIT_TOOL_RESULT_NAME and "fact-k" in message.text_content
        for message in final.messages
    )
    # Exactly one call past the limit.
    assert sum(not request.tools for request in provider.requests) == 1
    events = await runtime.events.history(result.run_id)
    assert events[-1].type == "run.completed"
    assert events[-1].data == {"outcome": "partial", "limit": "execution_steps"}
    audits = [a for a in await runtime.audits.list("t") if a.action == "run.limit_exceeded"]
    assert [audit.outcome for audit in audits] == ["summarize"]


async def test_summarize_at_the_model_call_and_token_limits() -> None:
    provider = _provider()
    runtime = _runtime(provider, budget=Budget(max_model_calls=3, on_limit="summarize"))
    result = await runtime.run(_request())
    assert (result.status, result.partial, result.limit_reached) == (
        RunStatus.COMPLETED,
        True,
        "model_calls",
    )
    assert result.execution_summary.model_calls == 4

    provider = _provider()
    runtime = _runtime(provider, budget=Budget(max_tokens=25, on_limit="summarize"))
    result = await runtime.run(_request())
    assert (result.status, result.partial, result.limit_reached) == (
        RunStatus.COMPLETED,
        True,
        "model_tokens",
    )
    # The answer is capped by its own output limit, not by the exhausted budget.
    assert provider.requests[-1].max_output_tokens == Budget().limit_answer_max_output_tokens


async def test_answer_at_the_limit_is_verified_without_a_repair_turn() -> None:
    class Rejects:
        name = "rejects"

        async def verify(self, value: Any, context: Any) -> VerificationResult:
            return VerificationResult(
                passed=False, confidence=1, reason_code="rejected", message="rejected"
            )

    provider = _provider()
    runtime = _runtime(
        provider,
        max_steps=3,
        budget=Budget(on_limit="summarize"),
        verification_policy=VerificationPolicy(verifiers=("rejects",), max_repairs=3),
    )
    runtime.verifiers.register(Rejects())
    result = await runtime.run(_request())

    assert result.status == RunStatus.FAILED
    assert result.error_type == "VerificationError"
    assert result.limit_reached == "execution_steps"
    assert result.partial is False
    assert sum(not request.tools for request in provider.requests) == 1


async def test_summarize_at_the_time_limit() -> None:
    provider = _provider()
    runtime = _runtime(
        provider,
        delay=5,
        budget=Budget(max_latency_seconds=0.2, on_limit="summarize"),
    )
    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED
    assert result.partial is True
    assert result.limit_reached == "time"
    final = provider.requests[-1]
    assert final.tools == ()
    assert final.timeout_seconds == Budget().limit_answer_timeout_seconds
    # The call that never got its result is left out of the transcript.
    assert not any(message.name == LIMIT_TOOL_RESULT_NAME for message in final.messages)


async def test_time_limit_without_summarize_still_times_out() -> None:
    runtime = _runtime(_provider(), delay=5, budget=Budget(max_latency_seconds=0.2))
    result = await runtime.run(_request())
    assert result.status == RunStatus.TIMED_OUT
    assert result.partial is False


async def test_answer_at_the_limit_that_overruns_its_time_fails_the_run() -> None:
    def handler(request: ModelRequest) -> ModelResponse:
        return _call()

    class SlowAnswer(MockModelProvider):
        async def generate(self, request: ModelRequest) -> ModelResponse:
            if not request.tools:
                await asyncio.sleep(5)
            return await super().generate(request)

    runtime = _runtime(
        SlowAnswer(handler=handler),
        max_steps=2,
        budget=Budget(on_limit="summarize", limit_answer_timeout_seconds=0.1),
    )
    state = await runtime.start(_request())
    await runtime.wait(state.id, "t")
    result = await runtime.wait(state.id, "t")
    assert result.status == RunStatus.FAILED
    assert result.limit_reached == "execution_steps"


async def test_continuation_policy_takes_precedence() -> None:
    runtime = _runtime(
        _provider(),
        budget=Budget(max_model_calls=2, on_limit="summarize"),
        continuation_policy=ContinuationPolicy(enabled=True, step_increment=2),
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.AWAITING_APPROVAL
    assert result.partial is False


def test_new_fields_are_left_out_while_unset() -> None:
    assert "on_limit" not in Budget().model_dump()
    assert Budget(on_limit="summarize").model_dump()["on_limit"] == "summarize"
    result = RunResult.model_validate(
        {
            "run_id": "r",
            "status": "completed",
            "session_id": "s",
            "execution_summary": {},
        }
    )
    dumped = json.loads(result.model_dump_json())
    assert not {"error_type", "limit_reached", "partial"} & dumped.keys()
    partial = result.model_copy(update={"partial": True, "limit_reached": "model_tokens"})
    assert json.loads(partial.model_dump_json())["partial"] is True


def test_limit_answer_messages_drop_unanswered_calls() -> None:
    call = ToolCall(id="a", name="data.lookup", arguments={"key": "x"})
    lost = ToolCall(id="b", name="data.lookup", arguments={"key": "y"})
    messages = [
        Message.text(Role.SYSTEM, "sys"),
        Message.text(Role.USER, "question"),
        Message(role=Role.ASSISTANT, content=(), tool_calls=(call, lost)),
        Message.text(Role.TOOL, '{"value": 1}').model_copy(
            update={"tool_call_id": "a", "name": "data.lookup"}
        ),
    ]
    transcript = limit_answer_messages(messages, "model_tokens")
    assert [message.role for message in transcript] == [
        Role.SYSTEM,
        Role.USER,
        Role.ASSISTANT,
        Role.USER,
        Role.USER,
    ]
    assert '"y"' not in transcript[2].text_content
    assert "model tokens limit" in transcript[-1].text_content

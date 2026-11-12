"""context_policy.max_context_tokens: an agent's own context budget for the built-in builders."""

from __future__ import annotations

import json

import pytest
from conftest import make_agent
from pydantic import ValidationError
from test_oversized_tool_result import _provider, _request, _runtime, _tool_messages

from algen_agent_runtime.context.builder import DefaultContextBuilder, RetrievalContextBuilder
from algen_agent_runtime.context.packing import SHORTENED_TOOL_RESULT_NOTE
from algen_agent_runtime.persistence.memory import InMemoryMemoryStore
from algen_agent_runtime.types.contracts import (
    AgentDefinition,
    ContextPolicy,
    Message,
    ModelProfile,
    Role,
    RunRequest,
    RunState,
    RunStatus,
    ToolCall,
)


def exchange(call_id: str, chars: int) -> list[Message]:
    call = ToolCall(id=call_id, name="records.lookup", arguments={"id": call_id})
    return [
        Message(role=Role.ASSISTANT, content=(), tool_calls=(call,)),
        Message.text(Role.TOOL, f"{call_id}:" + "x" * chars).model_copy(
            update={"tool_call_id": call_id}
        ),
    ]


def long_state(rounds: int = 12, chars: int = 9_000) -> RunState:
    """About 2,250 estimated tokens per result: 12 rounds need ~27,000, past the default 16,000."""
    state = RunState(
        request=RunRequest(agent="test-agent", input="payslips", tenant_id="t", user_id="u"),
        session_id="session",
    )
    state.messages = [
        Message.text(Role.USER, "payslips"),
        *(m for index in range(rounds) for m in exchange(f"call_{index}", chars)),
    ]
    return state


def tool_ids(messages: object) -> list[str | None]:
    return [m.tool_call_id for m in messages if m.role == Role.TOOL]  # type: ignore[attr-defined]


def budget(tokens: int | None = None, **policy: object) -> AgentDefinition:
    return make_agent(context_policy=ContextPolicy(max_context_tokens=tokens, **policy))


def test_unset_keeps_the_builders_budget_and_the_agents_serialised_form() -> None:
    assert ContextPolicy().max_context_tokens is None
    assert "max_context_tokens" not in ContextPolicy().model_dump()
    assert "max_context_tokens" not in make_agent().model_dump(mode="json")["context_policy"]
    # So an agent that does not set it fingerprints as it did in 0.2.0b2.
    assert make_agent().model_dump_json() == budget(None).model_dump_json()
    assert DefaultContextBuilder(InMemoryMemoryStore()).token_budget(make_agent()) == 16_000
    assert DefaultContextBuilder(InMemoryMemoryStore(), 24_000).token_budget() == 24_000
    assert budget(80_000).model_dump()["context_policy"]["max_context_tokens"] == 80_000


@pytest.mark.parametrize("tokens", [0, 1_023, 2_000_001])
def test_the_budget_is_bounded(tokens: int) -> None:
    with pytest.raises(ValidationError):
        ContextPolicy(max_context_tokens=tokens)


async def test_an_agent_budget_replaces_the_default_builders() -> None:
    builder = DefaultContextBuilder(InMemoryMemoryStore())
    default = await builder.build(long_state(), make_agent())
    larger = await builder.build(long_state(), budget(80_000))
    smaller = await builder.build(long_state(), budget(4_000))

    assert len(tool_ids(default)) < 12
    assert tool_ids(larger) == [f"call_{index}" for index in range(12)]
    assert len(tool_ids(smaller)) < len(tool_ids(default))
    assert sum(builder.estimate(m) for m in smaller) <= 4_000
    assert builder.token_budget(budget(80_000)) == 80_000


async def test_a_retrieval_builder_takes_the_agent_budget_too() -> None:
    class NoEvidence:
        async def retrieve(self, query: object) -> list[object]:
            return []

    builder = RetrievalContextBuilder(
        "evidence", InMemoryMemoryStore(), NoEvidence(), max_context_tokens=16_000
    )
    default = await builder.build(long_state(), make_agent())
    larger = await builder.build(long_state(), budget(80_000))
    assert len(tool_ids(default)) < 12 == len(tool_ids(larger))
    assert builder.token_budget(budget(80_000)) == 80_000


async def test_shortening_works_against_the_agent_budget() -> None:
    # A builder with room for everything; the agent's own, smaller budget decides.
    builder = DefaultContextBuilder(InMemoryMemoryStore(), 200_000)
    plain = await builder.build(long_state(), budget(8_000))
    shortened = await builder.build(
        long_state(),
        budget(8_000, shorten_older_tool_results=True, shortened_tool_result_chars=200),
    )
    assert len(tool_ids(shortened)) > len(tool_ids(plain))
    assert any(m.text_content.endswith(SHORTENED_TOOL_RESULT_NOTE) for m in shortened)
    assert sum(builder.estimate(m) for m in shortened) <= 8_000


async def test_a_result_too_large_for_the_default_fits_a_larger_agent_budget() -> None:
    # ~60,000 estimated tokens: cut under the default 16,000, sent whole under 80,000.
    provider = _provider()
    runtime = _runtime(provider, max_context_tokens=80_000)
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED, result.error
    [message] = _tool_messages(provider.requests[-1])
    assert "truncated" not in json.loads(message.text_content)
    events = await runtime.events.history(result.run_id)
    assert "context.tool_result_truncated" not in [event.type for event in events]


async def test_oversized_results_are_cut_to_the_agent_budget() -> None:
    kept: dict[int | None, int] = {}
    for tokens in (None, 8_000):
        provider = _provider()
        result = await _runtime(provider, max_context_tokens=tokens).run(_request())
        assert result.status == RunStatus.COMPLETED, result.error
        [message] = _tool_messages(provider.requests[-1])
        kept[tokens] = json.loads(message.text_content)["kept_chars"]
    assert kept[8_000] < kept[None]


async def test_the_model_context_window_still_bounds_what_is_sent() -> None:
    # The agent budget is larger than the model's window: the router packs to the window.
    provider = _provider()
    runtime = _runtime(provider, max_context_tokens=80_000)
    agent = runtime.agents.get("test-agent")
    windowed = agent.model_copy(
        update={
            "default_model": ModelProfile(
                name="test",
                provider="mock",
                model="deterministic",
                context_window_tokens=20_000,
            ),
            "budget": agent.budget.model_copy(update={"max_output_tokens": 1_000}),
            "version": "1.0.1",
        }
    )
    runtime.agents.register(windowed)
    result = await runtime.run(_request())
    sent = provider.requests[-1]
    estimate = sum(DefaultContextBuilder.estimate(message) for message in sent.messages)
    # Under the window, though the whole ~60,000-token result fits the agent's 80,000.
    assert estimate <= 20_000
    assert result.status in {RunStatus.COMPLETED, RunStatus.FAILED}

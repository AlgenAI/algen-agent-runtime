from __future__ import annotations

import pytest
from conftest import make_agent
from pydantic import ValidationError

from algen_agent_runtime.context.builder import DefaultContextBuilder
from algen_agent_runtime.context.packing import (
    SHORTENED_TOOL_RESULT_NOTE,
    pack_messages,
    shorten_tool_group,
)
from algen_agent_runtime.persistence.memory import InMemoryMemoryStore
from algen_agent_runtime.types.contracts import (
    ContextPolicy,
    Message,
    Role,
    RunRequest,
    RunState,
    ToolCall,
)

LONG = "row " * 2_000


def exchange(call_id: str, result: str = LONG) -> list[Message]:
    call = ToolCall(id=call_id, name="records.lookup", arguments={"id": call_id})
    return [
        Message(role=Role.ASSISTANT, content=(), tool_calls=(call,)),
        Message.text(Role.TOOL, result).model_copy(update={"tool_call_id": call_id}),
    ]


def transcript() -> list[Message]:
    return [
        Message.text(Role.SYSTEM, "instructions"),
        Message.text(Role.USER, "compare three records"),
        *exchange("a"),
        *exchange("b"),
        *exchange("c"),
    ]


def budget_for(messages: list[Message]) -> int:
    return sum(DefaultContextBuilder.estimate(message) for message in messages)


def test_off_by_default_older_tool_exchanges_are_dropped_whole() -> None:
    messages = transcript()
    # Room for the required messages and one more full exchange only.
    budget = budget_for([*messages[:2], *messages[-4:]]) + 10
    packed = pack_messages(messages, budget)
    assert [m.tool_call_id for m in packed if m.role == Role.TOOL] == ["b", "c"]
    assert all(SHORTENED_TOOL_RESULT_NOTE not in m.text_content for m in packed)


def test_shortening_keeps_older_exchanges_with_their_results() -> None:
    messages = transcript()
    budget = budget_for([*messages[:2], *messages[-4:]]) + 400
    packed = pack_messages(messages, budget, shorten_tool_results_to=200)

    results = [m for m in packed if m.role == Role.TOOL]
    assert [m.tool_call_id for m in results] == ["a", "b", "c"]
    # Recent results stay whole; only the one that no longer fits is shortened and marked.
    assert results[-1].text_content == LONG
    assert results[0].text_content.endswith(SHORTENED_TOOL_RESULT_NOTE)
    assert results[0].text_content.startswith(LONG[:200])
    # Every tool call still travels with its result.
    calls = [m for m in packed if m.tool_calls]
    assert [m.tool_calls[0].id for m in calls] == ["a", "b", "c"]
    assert budget_for(list(packed)) <= budget


def test_packing_stops_when_even_the_shortened_exchange_does_not_fit() -> None:
    messages = transcript()
    budget = budget_for([*messages[:2], *messages[-2:]]) + 5
    packed = pack_messages(messages, budget, shorten_tool_results_to=200)
    assert [m.tool_call_id for m in packed if m.role == Role.TOOL] == ["c"]


def test_only_tool_exchanges_with_long_text_are_shortened() -> None:
    assert shorten_tool_group([Message.text(Role.USER, LONG)], 100) is None
    assert shorten_tool_group(exchange("x", "short"), 100) is None
    shortened = shorten_tool_group(exchange("x"), 100)
    assert shortened is not None
    assert shortened[0] == exchange("x")[0]
    assert shortened[1].tool_call_id == "x"


async def test_agent_context_policy_turns_shortening_on() -> None:
    history = transcript()[1:]
    state = RunState(
        request=RunRequest(
            agent="test-agent", input="compare three records", tenant_id="tenant", user_id="u"
        ),
        session_id="session",
    )
    state.messages = history
    budget = budget_for([*transcript()[:2], *history[-4:]]) + 400
    builder = DefaultContextBuilder(InMemoryMemoryStore(), budget)

    plain = await builder.build(state, make_agent())
    shortening = await builder.build(
        state,
        make_agent(
            context_policy=ContextPolicy(
                shorten_older_tool_results=True, shortened_tool_result_chars=200
            )
        ),
    )
    assert len([m for m in shortening if m.role == Role.TOOL]) > len(
        [m for m in plain if m.role == Role.TOOL]
    )


@pytest.mark.parametrize("chars", [0, 63, 16_385])
def test_shortened_length_is_bounded(chars: int) -> None:
    with pytest.raises(ValidationError):
        ContextPolicy(shorten_older_tool_results=True, shortened_tool_result_chars=chars)

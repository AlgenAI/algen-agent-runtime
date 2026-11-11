from __future__ import annotations

import pytest
from conftest import make_agent
from pydantic import ValidationError

from algen_agent_runtime.context.builder import DefaultContextBuilder, RetrievalContextBuilder
from algen_agent_runtime.context.packing import (
    REPAIR_MESSAGE_NAME,
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
    TextBlock,
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


def test_an_exchange_is_dropped_when_even_its_shortened_form_does_not_fit() -> None:
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


def run_state(history: list[Message]) -> RunState:
    state = RunState(
        request=RunRequest(
            agent="test-agent", input="compare three records", tenant_id="tenant", user_id="u"
        ),
        session_id="session",
    )
    state.messages = history
    return state


async def test_builders_overriding_pack_with_one_argument_still_build() -> None:
    # Subclasses written before the policy argument existed override ``pack(self, messages)``.
    class LegacyBuilder(DefaultContextBuilder):
        def pack(self, messages):  # type: ignore[override]
            self.packed = len(messages)
            return super().pack(messages)

    history = transcript()[1:]
    builder = LegacyBuilder(InMemoryMemoryStore(), 100_000)
    built = await builder.build(run_state(history), make_agent())
    assert builder.packed == len(built) == len(history) + 1


async def test_retrieval_builder_honours_the_policy() -> None:
    class NoEvidence:
        async def retrieve(self, query):
            return []

    history = transcript()[1:]
    budget = budget_for([*transcript()[:2], *history[-4:]]) + 400
    builder = RetrievalContextBuilder(
        "evidence", InMemoryMemoryStore(), NoEvidence(), max_context_tokens=budget
    )
    agent = make_agent(
        context_policy=ContextPolicy(
            shorten_older_tool_results=True, shortened_tool_result_chars=200
        )
    )
    plain = await builder.build(run_state(history), make_agent())
    shortening = await builder.build(run_state(history), agent)
    assert len([m for m in shortening if m.role == Role.TOOL]) > len(
        [m for m in plain if m.role == Role.TOOL]
    )


def long_run(rounds: int = 12, chars: int = 9_000) -> list[Message]:
    return [
        Message.text(Role.SYSTEM, "instructions"),
        Message.text(Role.USER, "payslips for everyone since joining"),
        *(
            message
            for index in range(rounds)
            for message in exchange(f"call_{index}", f"{index}:" + "x" * chars)
        ),
    ]


def assert_calls_travel_with_results(packed: tuple[Message, ...]) -> None:
    pending: set[str] = set()
    for message in packed:
        if message.tool_calls:
            pending = {call.id for call in message.tool_calls}
        elif message.role == Role.TOOL:
            assert message.tool_call_id in pending
            pending.discard(message.tool_call_id or "")
    calls = {call.id for message in packed for call in message.tool_calls}
    results = {message.tool_call_id for message in packed if message.role == Role.TOOL}
    assert calls == results


def test_long_run_keeps_the_oldest_result_shortened() -> None:
    policy = ContextPolicy(shorten_older_tool_results=True)
    packed = DefaultContextBuilder(InMemoryMemoryStore(), 16_000).pack(long_run(), policy)

    results = {m.tool_call_id: m.text_content for m in packed if m.role == Role.TOOL}
    assert set(results) == {f"call_{index}" for index in range(12)}
    assert results["call_0"].startswith("0:")
    assert results["call_0"].endswith(policy.shortened_tool_result_note)
    assert len(results["call_0"]) == 1_200 + len(policy.shortened_tool_result_note)
    assert results["call_11"] == "11:" + "x" * 9_000
    assert packed[1].text_content == "payslips for everyone since joining"
    assert_calls_travel_with_results(packed)
    assert budget_for(list(packed)) <= 16_000


def test_whole_results_keep_most_of_the_budget_newest_first() -> None:
    policy = ContextPolicy(shorten_older_tool_results=True, shortened_tool_results_share=0.25)
    packed = DefaultContextBuilder(InMemoryMemoryStore(), 16_000).pack(long_run(), policy)
    whole = [
        m.tool_call_id
        for m in packed
        if m.role == Role.TOOL and not m.text_content.endswith(policy.shortened_tool_result_note)
    ]
    # Whole results are a contiguous newest run; everything older is shortened.
    assert whole == [f"call_{index}" for index in range(12 - len(whole), 12)]
    assert len(whole) >= 4


def test_without_shortening_the_long_run_drops_the_oldest_exchanges() -> None:
    packed = DefaultContextBuilder(InMemoryMemoryStore(), 16_000).pack(long_run())
    assert "call_0" not in {m.tool_call_id for m in packed}
    assert_calls_travel_with_results(packed)


def test_small_runs_are_unchanged() -> None:
    messages = long_run(rounds=3, chars=500)
    packed = pack_messages(messages, 16_000, shorten_tool_results_to=64)
    assert packed == tuple(messages)


def test_a_stray_tool_result_is_dropped_not_raised() -> None:
    stray = Message.text(Role.TOOL, "orphan").model_copy(update={"tool_call_id": "gone"})
    messages = long_run(rounds=2, chars=100)
    messages.insert(2, stray)
    policy = ContextPolicy(shorten_older_tool_results=True)
    packed = DefaultContextBuilder(InMemoryMemoryStore(), 16_000).pack(messages, policy)
    assert stray not in packed
    assert [m.tool_call_id for m in packed if m.role == Role.TOOL] == ["call_0", "call_1"]


def test_the_shortened_note_is_configurable_and_can_be_left_out() -> None:
    note = " [earlier result shortened to save space; it was already retrieved in this task]"
    for configured in (note, ""):
        policy = ContextPolicy(
            shorten_older_tool_results=True, shortened_tool_result_note=configured
        )
        packed = DefaultContextBuilder(InMemoryMemoryStore(), 16_000).pack(long_run(), policy)
        oldest = next(m for m in packed if m.tool_call_id == "call_0").text_content
        assert oldest == "0:" + "x" * 1_198 + configured


def test_an_exchange_too_large_even_shortened_is_skipped_and_older_ones_kept() -> None:
    messages = long_run(rounds=12)
    # An old exchange whose arguments alone are larger than the budget left for it.
    big = ToolCall(id="big", name="records.lookup", arguments={"id": "y" * 20_000})
    messages.insert(4, Message(role=Role.ASSISTANT, content=(), tool_calls=(big,)))
    messages.insert(5, Message.text(Role.TOOL, "ok").model_copy(update={"tool_call_id": "big"}))
    policy = ContextPolicy(shorten_older_tool_results=True)
    packed = DefaultContextBuilder(InMemoryMemoryStore(), 16_000).pack(messages, policy)
    kept = {m.tool_call_id for m in packed if m.role == Role.TOOL}
    assert "big" not in kept
    assert {"call_0", "call_11"} <= kept
    assert_calls_travel_with_results(packed)


def test_the_request_that_started_the_run_survives_a_repair_turn() -> None:
    messages = long_run(rounds=12)
    messages.append(Message(role=Role.ASSISTANT, content=(TextBlock(text="draft"),)))
    messages.append(
        Message.text(Role.USER, "repair the answer").model_copy(
            update={"name": REPAIR_MESSAGE_NAME}
        )
    )
    packed = pack_messages(messages, 4_000)
    assert packed[1].text_content == "payslips for everyone since joining"
    assert packed[-1].name == REPAIR_MESSAGE_NAME


@pytest.mark.parametrize("share", [0, 1, 1.5])
def test_shortened_share_is_bounded(share: float) -> None:
    with pytest.raises(ValidationError):
        ContextPolicy(shortened_tool_results_share=share)

from algen_agent_runtime.context.builder import DefaultContextBuilder
from algen_agent_runtime.persistence.memory import InMemoryMemoryStore
from algen_agent_runtime.types.contracts import Message, Role, TextBlock, ToolCall


def _tool_round(call_id: str, size: int) -> list[Message]:
    return [
        Message(
            role=Role.ASSISTANT,
            content=(),
            tool_calls=(ToolCall(id=call_id, name="lookup", arguments={}),),
        ),
        Message(role=Role.TOOL, tool_call_id=call_id, content=(TextBlock(text="x" * size),)),
    ]


def _unanswered_calls(messages: tuple[Message, ...]) -> list[str]:
    answered = {message.tool_call_id for message in messages if message.role == Role.TOOL}
    return [call.id for message in messages for call in message.tool_calls if call.id not in answered]


def _orphan_results(messages: tuple[Message, ...]) -> list[str | None]:
    requested = {call.id for message in messages for call in message.tool_calls}
    return [m.tool_call_id for m in messages if m.role == Role.TOOL and m.tool_call_id not in requested]


def test_trimming_never_separates_a_tool_call_from_its_result() -> None:
    messages = [Message.text(Role.SYSTEM, "system"), Message.text(Role.USER, "read many records")]
    for index in range(12):
        messages += _tool_round(f"call_{index}", 9_000)

    packed = DefaultContextBuilder(InMemoryMemoryStore(), max_estimated_tokens=16_000).pack(messages)

    assert _unanswered_calls(packed) == []
    assert _orphan_results(packed) == []
    assert "call_11" in {m.tool_call_id for m in packed if m.role == Role.TOOL}


def test_small_transcripts_are_unchanged_and_stray_results_dropped() -> None:
    messages = [Message.text(Role.SYSTEM, "system"), Message.text(Role.USER, "hi"), *_tool_round("a", 40)]
    assert DefaultContextBuilder(InMemoryMemoryStore()).pack(messages) == tuple(messages)

    stray = [Message(role=Role.TOOL, tool_call_id="gone", content=(TextBlock(text="r"),)), Message.text(Role.USER, "hi")]
    assert [m.role for m in DefaultContextBuilder(InMemoryMemoryStore()).pack(stray)] == [Role.USER]

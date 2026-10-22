"""Session memory keeps whole tool exchanges: never a call without its result, or the reverse."""

from __future__ import annotations

from algen_agent_runtime.context.turn_memory import (
    TRUNCATION_MARKER,
    turn_messages,
    whole_tool_exchanges,
)
from algen_agent_runtime.types.contracts import Message, Role, TextBlock, ToolCall


def _call(call_id: str) -> Message:
    return Message(
        role=Role.ASSISTANT,
        content=(),
        tool_calls=(ToolCall(id=call_id, name="files.read", arguments={}),),
    )


def _result(call_id: str, text: str = "{}") -> Message:
    return Message(
        role=Role.TOOL, name="files.read", tool_call_id=call_id, content=(TextBlock(text=text),)
    )


def test_a_memory_window_starts_at_a_whole_turn() -> None:
    cut = [
        _result("a"),
        Message.text(Role.ASSISTANT, "old answer"),
        Message.text(Role.USER, "next"),
        _call("b"),
        _result("b"),
        Message.text(Role.ASSISTANT, "answer"),
    ]
    assert [m.role for m in whole_tool_exchanges(cut)] == [
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
        Role.ASSISTANT,
    ]
    assert whole_tool_exchanges([_result("a")]) == []


def test_a_call_without_its_result_is_dropped_and_vice_versa() -> None:
    messages = [
        Message.text(Role.USER, "q"),
        _call("a"),
        _result("x"),
        Message.text(Role.ASSISTANT, "done"),
    ]
    assert [m.role for m in whole_tool_exchanges(messages)] == [Role.USER, Role.ASSISTANT]


def test_a_turn_keeps_its_question_tool_steps_and_answer_with_results_capped() -> None:
    history = [
        Message.text(Role.SYSTEM, "sys"),
        Message.text(Role.USER, "earlier"),
        Message.text(Role.ASSISTANT, "earlier answer"),
        Message.text(Role.USER, "read it"),
        _call("a"),
        _result("a", "x" * 600),
        Message.text(Role.ASSISTANT, "draft"),
    ]
    kept = turn_messages(history, "read it", "final", max_chars=256)
    assert [m.role for m in kept] == [Role.USER, Role.ASSISTANT, Role.TOOL, Role.ASSISTANT]
    assert kept[0].text_content == "read it" and kept[-1].text_content == "final"
    assert kept[2].text_content == "x" * 256 + TRUNCATION_MARKER

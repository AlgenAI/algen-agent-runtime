"""What a turn leaves in session memory when tool steps are kept.

By default memory keeps a turn's question and final answer. With
``MemoryPolicy.store_tool_payloads`` it also keeps the turn's tool calls and their results,
so a follow-up question can build on what the agent read. Tool results are capped at
``MemoryPolicy.tool_payload_max_chars``. A tool call is only ever kept together with its
results, and history read back starts at a whole turn: providers reject a tool result
without the call that asked for it.
"""

from __future__ import annotations

from collections.abc import Sequence

from algen_agent_runtime.types.contracts import Message, Role, TextBlock

TRUNCATION_MARKER = "… [truncated for memory]"


def turn_messages(
    history: Sequence[Message], user_input: str, output_text: str, max_chars: int
) -> list[Message]:
    """The turn's question, its tool calls and results, and its final answer."""
    start = next(
        (
            index
            for index in range(len(history) - 1, -1, -1)
            if history[index].role == Role.USER and history[index].text_content == user_input
        ),
        None,
    )
    kept: list[Message] = [Message.text(Role.USER, user_input)]
    if start is not None:
        for message in history[start + 1 :]:
            if message.role == Role.ASSISTANT and message.tool_calls:
                kept.append(message)
            elif message.role == Role.TOOL:
                kept.append(_capped(message, max_chars))
            elif message.role == Role.USER:
                kept.append(message)  # a clarification given during the turn
    if output_text:
        kept.append(Message.text(Role.ASSISTANT, output_text))
    return whole_tool_exchanges(kept)


def whole_tool_exchanges(messages: Sequence[Message]) -> list[Message]:
    """Drop a leading partial turn, and any tool call or result missing its other half."""
    first_user = next(
        (index for index, message in enumerate(messages) if message.role == Role.USER), None
    )
    if first_user is None:
        return []
    messages = list(messages[first_user:])
    answered = {message.tool_call_id for message in messages if message.role == Role.TOOL}
    complete: list[Message] = []
    asked: set[str] = set()
    for message in messages:
        if message.role == Role.ASSISTANT and message.tool_calls:
            if not all(call.id in answered for call in message.tool_calls):
                continue
            asked.update(call.id for call in message.tool_calls)
        elif message.role == Role.TOOL and message.tool_call_id not in asked:
            continue
        complete.append(message)
    return complete


def _capped(message: Message, max_chars: int) -> Message:
    text = message.text_content
    if len(text) <= max_chars:
        return message
    return message.model_copy(
        update={"content": (TextBlock(text=text[:max_chars] + TRUNCATION_MARKER),)}
    )

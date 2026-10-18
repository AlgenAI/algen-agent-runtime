from __future__ import annotations

from collections.abc import Sequence

from algen_agent_runtime.exceptions.errors import BudgetExceededError, ConflictError
from algen_agent_runtime.types.contracts import Message, Role

CONTEXT_SUMMARY_NAME = "runtime_context_summary"


def estimate_message(message: Message) -> int:
    """Dependency-free estimate including tool arguments and content metadata.

    This bounds our estimate, not a provider's tokenizer or image/file token cost.
    """
    return max(1, (len(message.model_dump_json(exclude_none=True)) + 3) // 4)


def message_groups(messages: Sequence[Message]) -> list[list[Message]]:
    # Keep a model's tool-call message and all its results as one atomic group.
    # A group can be pending while the runtime executes a multi-tool response.
    groups: list[list[Message]] = []
    for message in messages:
        if message.role == Role.TOOL:
            if not groups or message.tool_call_id not in {
                call.id for call in groups[-1][0].tool_calls
            }:
                raise ConflictError("context contains a tool result without its tool call")
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups


def pack_messages(messages: Sequence[Message], max_estimated_tokens: int) -> tuple[Message, ...]:
    if not messages:
        return ()
    groups = message_groups(messages)
    required = {
        index
        for index, group in enumerate(groups)
        if any(
            message.role == Role.SYSTEM
            or (message.role == Role.ASSISTANT and message.name == CONTEXT_SUMMARY_NAME)
            for message in group
        )
    }
    required.add(len(groups) - 1)
    for index in range(len(groups) - 1, -1, -1):
        if groups[index][0].role == Role.USER:
            required.add(index)
            break
    costs = [sum(estimate_message(message) for message in group) for group in groups]
    remaining = max_estimated_tokens - sum(costs[index] for index in required)
    if remaining < 0:
        raise BudgetExceededError(
            "required context exceeds max_estimated_tokens; increase the context limit "
            "or reduce the input/tool payload"
        )
    selected = set(required)
    for index in range(len(groups) - 1, -1, -1):
        if index in required:
            continue
        if costs[index] > remaining:
            break
        selected.add(index)
        remaining -= costs[index]
    return tuple(
        message for index, group in enumerate(groups) if index in selected for message in group
    )

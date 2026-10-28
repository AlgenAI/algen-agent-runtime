from __future__ import annotations

from collections.abc import Sequence

from algen_agent_runtime.exceptions.errors import BudgetExceededError, ConflictError
from algen_agent_runtime.types.contracts import Message, Role, TextBlock

CONTEXT_SUMMARY_NAME = "runtime_context_summary"
WORKING_STATE_NAME = "runtime_working_state"
SKILL_CATALOG_NAME = "runtime_skill_catalog"
LOADED_SKILLS_NAME = "runtime_loaded_skills"
SHORTENED_TOOL_RESULT_NOTE = (
    "\n[earlier tool result shortened to fit the context; it was already retrieved in this run]"
)


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


def shorten_tool_group(group: Sequence[Message], max_chars: int) -> list[Message] | None:
    """The tool group with each long text result cut to ``max_chars`` and marked as shortened.

    The tool-call message and every result stay, so the group remains a valid exchange and the
    model still knows what it already retrieved. ``None`` when the group is not a tool exchange
    or nothing in it is longer than ``max_chars``.
    """
    head = group[0]
    if head.role != Role.ASSISTANT or not head.tool_calls:
        return None
    shortened = [head]
    changed = False
    for message in group[1:]:
        blocks = []
        for block in message.content:
            if isinstance(block, TextBlock) and len(block.text) > max_chars:
                block = TextBlock(text=block.text[:max_chars] + SHORTENED_TOOL_RESULT_NOTE)
                changed = True
            blocks.append(block)
        shortened.append(message.model_copy(update={"content": tuple(blocks)}))
    return shortened if changed else None


def pack_messages(
    messages: Sequence[Message],
    max_estimated_tokens: int,
    *,
    shorten_tool_results_to: int | None = None,
) -> tuple[Message, ...]:
    """The newest messages that fit ``max_estimated_tokens``, keeping required context.

    Groups are added from newest to oldest until one does not fit. With
    ``shorten_tool_results_to``, an older tool exchange that does not fit is shortened instead
    (each long result cut to that many characters) and packing continues while the shortened
    version fits, so a long run keeps a record of what it already looked up.
    """
    if not messages:
        return ()
    groups = message_groups(messages)
    required = {
        index
        for index, group in enumerate(groups)
        if any(
            message.role == Role.SYSTEM
            or (
                message.role == Role.ASSISTANT
                and message.name
                in {
                    CONTEXT_SUMMARY_NAME,
                    WORKING_STATE_NAME,
                    SKILL_CATALOG_NAME,
                    LOADED_SKILLS_NAME,
                }
            )
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
    shortened: dict[int, list[Message]] = {}
    for index in range(len(groups) - 1, -1, -1):
        if index in required:
            continue
        if costs[index] <= remaining:
            selected.add(index)
            remaining -= costs[index]
            continue
        short = (
            shorten_tool_group(groups[index], shorten_tool_results_to)
            if shorten_tool_results_to is not None
            else None
        )
        cost = sum(estimate_message(message) for message in short) if short else 0
        if short is None or cost > remaining:
            break
        shortened[index] = short
        selected.add(index)
        remaining -= cost
    return tuple(
        message
        for index, group in enumerate(groups)
        if index in selected
        for message in shortened.get(index, group)
    )

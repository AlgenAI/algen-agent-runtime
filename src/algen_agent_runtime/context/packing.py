from __future__ import annotations

import logging
from collections.abc import Sequence

from algen_agent_runtime.exceptions.errors import BudgetExceededError, ConflictError
from algen_agent_runtime.runtime.attachments import IMAGE_TOKENS, PDF_TOKENS
from algen_agent_runtime.types.contracts import (
    DEFAULT_SHORTENED_TOOL_RESULT_NOTE,
    FileBlock,
    Message,
    Role,
    TextBlock,
)

LOGGER = logging.getLogger(__name__)

CONTEXT_SUMMARY_NAME = "runtime_context_summary"
# The user-role turn that asks the model to repair an answer that failed verification. It is the
# Runtime's, not the user's: hosts that show history can filter it out by this name.
REPAIR_MESSAGE_NAME = "runtime_repair"
WORKING_STATE_NAME = "runtime_working_state"
SKILL_CATALOG_NAME = "runtime_skill_catalog"
LOADED_SKILLS_NAME = "runtime_loaded_skills"
SHORTENED_TOOL_RESULT_NOTE = DEFAULT_SHORTENED_TOOL_RESULT_NOTE


def estimate_message(message: Message) -> int:
    """Dependency-free estimate including tool arguments and content metadata.

    This bounds our estimate, not a provider's tokenizer. An attached image or PDF counts at a
    rough fixed cost, since only its reference is in the message.
    """
    files = sum(
        PDF_TOKENS if block.media_type == "application/pdf" else IMAGE_TOKENS
        for block in message.content
        if isinstance(block, FileBlock)
    )
    return max(1, (len(message.model_dump_json(exclude_none=True)) + 3) // 4) + files


def message_groups(messages: Sequence[Message], *, strict: bool = False) -> list[list[Message]]:
    """Messages grouped so a model's tool-call message travels with all its results.

    A group can be pending while the runtime executes a multi-tool response. A tool result whose
    call is not in the group before it (left by a host's edited or imported history) is dropped
    with a warning: sending it would make most providers reject the request, and failing the run
    over history the user cannot fix helps no one. ``strict`` raises ``ConflictError`` instead,
    for checks that report a broken transcript.
    """
    groups: list[list[Message]] = []
    for message in messages:
        if message.role == Role.TOOL:
            if not groups or message.tool_call_id not in {
                call.id for call in groups[-1][0].tool_calls
            }:
                if strict:
                    raise ConflictError("context contains a tool result without its tool call")
                LOGGER.warning(
                    "dropping a tool result without its tool call from the context",
                    extra={"tool_call_id": message.tool_call_id},
                )
                continue
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups


def shorten_tool_group(
    group: Sequence[Message], max_chars: int, note: str = SHORTENED_TOOL_RESULT_NOTE
) -> list[Message] | None:
    """The tool group with each long text result cut to ``max_chars`` and ended with ``note``.

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
                block = TextBlock(text=block.text[:max_chars] + note)
                changed = True
            blocks.append(block)
        shortened.append(message.model_copy(update={"content": tuple(blocks)}))
    return shortened if changed else None


_REQUIRED_NAMES = frozenset(
    {CONTEXT_SUMMARY_NAME, WORKING_STATE_NAME, SKILL_CATALOG_NAME, LOADED_SKILLS_NAME}
)


def _required_groups(groups: Sequence[Sequence[Message]]) -> set[int]:
    required = {
        index
        for index, group in enumerate(groups)
        if any(
            message.role == Role.SYSTEM
            or (message.role == Role.ASSISTANT and message.name in _REQUIRED_NAMES)
            for message in group
        )
    }
    required.add(len(groups) - 1)
    # The latest user turn, and the request that started the run: a repair turn is the
    # Runtime's, so the user turn before it is kept too.
    for index in range(len(groups) - 1, -1, -1):
        if groups[index][0].role == Role.USER:
            required.add(index)
            if groups[index][0].name != REPAIR_MESSAGE_NAME:
                break
    return required


def pack_messages(
    messages: Sequence[Message],
    max_estimated_tokens: int,
    *,
    shorten_tool_results_to: int | None = None,
    shortened_share: float = 0.25,
    shortened_note: str = SHORTENED_TOOL_RESULT_NOTE,
) -> tuple[Message, ...]:
    """The newest messages that fit ``max_estimated_tokens``, keeping required context.

    Required context is the system messages, the Runtime's summary, working-state and skill
    messages, the newest group and the user request that started the run.

    Without ``shorten_tool_results_to``, groups are added from newest to oldest until one does
    not fit. With it, and only when the history does not fit whole, ``shortened_share`` of the
    free budget is held back for older tool exchanges: whole groups fill the rest, newest first,
    and every older tool exchange is then kept with each long result cut to
    ``shorten_tool_results_to`` characters and ended with ``shortened_note``. An exchange is
    dropped only when even its shortened form does not fit, and packing then carries on with
    older ones. A tool call is never separated from its results.
    """
    groups = message_groups(messages)
    if not groups:
        return ()
    required = _required_groups(groups)
    costs = [sum(estimate_message(message) for message in group) for group in groups]
    remaining = max_estimated_tokens - sum(costs[index] for index in required)
    if remaining < 0:
        raise BudgetExceededError(
            "required context exceeds max_estimated_tokens; increase the context limit "
            "or reduce the input/tool payload"
        )
    selected = set(required)
    shortened: dict[int, list[Message]] = {}
    optional = [index for index in range(len(groups) - 1, -1, -1) if index not in required]
    if shorten_tool_results_to is None or sum(costs[index] for index in optional) <= remaining:
        for index in optional:
            if costs[index] > remaining:
                break
            selected.add(index)
            remaining -= costs[index]
    else:
        reserve = int(remaining * shortened_share)
        whole = remaining - reserve
        older = len(optional)
        for position, index in enumerate(optional):
            if costs[index] > whole:
                older = position
                break
            selected.add(index)
            whole -= costs[index]
        remaining = whole + reserve
        for index in optional[older:]:
            short = shorten_tool_group(groups[index], shorten_tool_results_to, shortened_note)
            candidate = short if short is not None else groups[index]
            cost = sum(estimate_message(message) for message in candidate)
            if cost > remaining:
                continue
            if short is not None:
                shortened[index] = short
            selected.add(index)
            remaining -= cost
    return tuple(
        message
        for index, group in enumerate(groups)
        if index in selected
        for message in shortened.get(index, group)
    )

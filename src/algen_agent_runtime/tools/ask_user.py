"""``core.ask_user``: the model asks the user a question and the run waits for the answer."""

from __future__ import annotations

from typing import Any

from algen_agent_runtime.exceptions.errors import ToolExecutionError
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)

ASK_USER_TOOL = "core.ask_user"
ASK_USER_PERMISSION = "user.ask"
MAX_QUESTION_CHARS = 2_000
MAX_CHOICES = 20
MAX_CHOICE_CHARS = 200
MAX_ANSWER_CHARS = 10_000


def ask_user_tool() -> Tool:
    """``core.ask_user``: pause the run for a clarification from the user.

    The Runtime handles the call itself: the run pauses in ``awaiting_clarification`` with a
    ``clarification.required`` event whose payload is ``kind: ask_user`` with the ``question``,
    the optional ``choices`` and the ``tool_call_id``. ``resume(run_id, tenant_id,
    {"clarification": "..."})`` (or ``POST /v1/runs/{run_id}/clarification``) answers it: the
    model gets ``{"answer": "..."}`` as the call's result and the run goes on. ``choices`` are
    suggestions for a UI; the answer is free text.

    Every ``AgentRuntime`` registers it; an agent offers it to its model only with
    ``core.ask_user`` in ``enabled_tools`` and ``user.ask`` in ``tool_permissions``.
    """

    async def execute(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        # Only a run can wait for its user; a workflow tool step or a direct call cannot.
        raise ToolExecutionError(f"{ASK_USER_TOOL} runs only inside an agent run")

    return Tool(
        ToolDefinition(
            name=ASK_USER_TOOL,
            version="1.0.0",
            description=(
                "Ask the user a question and wait for the answer, when you cannot go on "
                "without information only the user has. Offer choices when the answer is one "
                "of a few options."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_QUESTION_CHARS,
                    },
                    "choices": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": MAX_CHOICE_CHARS},
                        "maxItems": MAX_CHOICES,
                    },
                },
                "required": ["question"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "required": ["answer"]},
            required_permissions=frozenset({ASK_USER_PERMISSION}),
            side_effect=SideEffect.NONE,
            # Each call asks again: never cached, retried or held back as a repeat.
            idempotency=Idempotency.NON_IDEMPOTENT,
            volatile=True,
        ),
        execute,
    )

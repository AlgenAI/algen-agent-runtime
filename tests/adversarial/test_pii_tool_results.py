"""PII in tool results: declared address fields reach the model, everything else is redacted."""

from __future__ import annotations

import pytest
from adversarial_harness import build, calls, request, seen_by_model

from algen_agent_runtime.tools.contracts import Tool, ToolDefinition
from algen_agent_runtime.types.contracts import RunStatus

pytestmark = pytest.mark.adversarial


async def test_the_model_sees_a_sender_address_but_not_addresses_in_the_body() -> None:
    """Threat: a mail body leaks third-party addresses to the model. Control: only the field the
    tool declares ``format: email`` is left as is (#83 item 6)."""
    inbox = Tool(
        ToolDefinition(
            name="mail.read",
            version="1",
            description="Read the inbox",
            input_schema={"type": "object"},
            output_schema={
                "type": "object",
                "properties": {
                    "from": {"type": "string", "format": "email"},
                    "body": {"type": "string"},
                },
            },
        ),
        lambda args, ctx: {"from": "kim@example.com", "body": "cc lee@example.net please"},
    )
    runtime, provider = build([calls(("mail.read", {})), "done"], [inbox])

    result = await runtime.run(request("Who wrote to me?"))

    assert result.status == RunStatus.COMPLETED
    seen = seen_by_model(provider)
    assert "kim@example.com" in seen
    assert "lee@example.net" not in seen

"""PII in tool results and arguments: declared address and identifier fields pass, everything
else is redacted."""

from __future__ import annotations

import pytest
from adversarial_harness import build, calls, request, seen_by_model, tool

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


_SEND_MESSAGE = {
    "type": "object",
    "properties": {
        "conversation_id": {"type": "string", "pattern": "^-?[0-9]{1,20}$", "x-pii": False},
        "text": {"type": "string"},
    },
    "required": ["conversation_id", "text"],
}


async def test_a_chat_id_and_an_order_number_do_not_refuse_a_message() -> None:
    """Threat: none; a false positive. A ten-digit private-chat id and an order number read as
    phone numbers, and a redacted argument refuses the call (RT-B13, RT-G08). Control: a bare
    ten-digit id is not a phone number, and the chat id's field is declared an identifier."""
    send, sent = tool("telegram.send_message", schema=_SEND_MESSAGE)
    arguments = {"conversation_id": "09876543210", "text": "Order 7012345678 has shipped."}
    runtime, _ = build([calls(("telegram.send_message", arguments)), "sent"], [send])

    result = await runtime.run(request("Tell the customer"))

    assert result.status == RunStatus.COMPLETED
    assert sent.calls == [arguments]


async def test_a_phone_number_in_the_message_text_still_refuses_the_call() -> None:
    """Threat: the model copies a customer's phone number into an outgoing message.
    Control: only the declared identifier field is exempt; the text is still screened."""
    send, sent = tool("telegram.send_message", schema=_SEND_MESSAGE)
    arguments = {"conversation_id": "9876543210", "text": "Ring Kim on +91 98765 43210"}
    runtime, _ = build([calls(("telegram.send_message", arguments))], [send])

    result = await runtime.run(request("Tell the customer"))

    assert result.status == RunStatus.FAILED
    assert sent.calls == []

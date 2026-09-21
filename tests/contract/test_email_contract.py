from __future__ import annotations

import smtplib
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from algen_agent_runtime.communications import (
    EmailAttachmentReference,
    EmailDeliveryError,
    EmailDeliveryReceipt,
    EmailDeliveryStatus,
    EmailMessage,
    SmtpEmailSender,
    SmtpSettings,
    email_tool,
)
from algen_agent_runtime.exceptions.errors import ToolExecutionError
from algen_agent_runtime.tools.contracts import ToolContext
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry


def message(**updates: Any) -> EmailMessage:
    values: dict[str, Any] = {
        "sender": {"address": "recruiting@algen.ai", "display_name": "Algen Recruiting"},
        "to": [{"address": "candidate@example.com"}],
        "subject": "Application update",
        "text_body": "Thank you for applying.",
    }
    values.update(updates)
    return EmailMessage.model_validate(values)


def test_email_contract_rejects_header_injection_duplicate_recipients_and_paths() -> None:
    with pytest.raises(ValidationError, match="line breaks"):
        message(subject="Application update\nBcc: attacker@example.com")
    with pytest.raises(ValidationError, match="unique"):
        message(cc=[{"address": "candidate@example.com"}])
    with pytest.raises(ValidationError, match="must not contain a path"):
        EmailAttachmentReference(
            artifact_id="artifact-1",
            filename="../resume.pdf",
            media_type="application/pdf",
        )
    with pytest.raises(ValidationError, match="plaintext SMTP"):
        SmtpSettings(host="smtp.example.com", security="plain")
    with pytest.raises(ValidationError, match="header name is invalid"):
        message(headers={"X-Valid: Bcc": "attacker@example.com"})
    with pytest.raises(ValidationError, match="media_type"):
        EmailAttachmentReference(
            artifact_id="artifact-1",
            filename="resume.txt",
            media_type="text/plain\r\nX-Evil: yes",
        )
    with pytest.raises(ValidationError, match="message ID domain"):
        SmtpSettings(host="smtp.example.com", message_id_domain="mail.example.com\r\nBcc")


class FakeSmtpClient:
    def __init__(self, *, refused: dict[str, tuple[int, bytes]] | None = None) -> None:
        self.refused = refused or {}
        self.messages: list[Any] = []
        self.recipients: tuple[str, ...] = ()
        self.started_tls = False
        self.closed = False

    def ehlo(self) -> None:
        pass

    def starttls(self, *, context: Any) -> None:
        self.started_tls = context is not None

    def login(self, username: str, password: str) -> None:
        assert username == "smtp-user"
        assert password == "smtp-password"

    def send_message(self, value: Any, *, to_addrs: tuple[str, ...]) -> Any:
        self.messages.append(value)
        self.recipients = to_addrs
        return self.refused

    def quit(self) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_smtp_adapter_uses_artifact_references_bcc_and_stable_message_id() -> None:
    client = FakeSmtpClient()

    async def load_artifact(reference: EmailAttachmentReference) -> bytes:
        assert reference.artifact_id == "artifact-1"
        return b"synthetic resume"

    sender = SmtpEmailSender(
        SmtpSettings(
            host="smtp.example.com",
            username="smtp-user",
            password="smtp-password",
            message_id_domain="mail.algen.ai",
        ),
        artifact_loader=load_artifact,
        smtp_factory=lambda settings: client,
    )
    value = message(
        bcc=[{"address": "audit@example.com"}],
        attachments=[
            {
                "artifact_id": "artifact-1",
                "filename": "resume.txt",
                "media_type": "text/plain",
            }
        ],
    )

    first = await sender.send(value, idempotency_key="application:42:decline")
    second = await sender.send(value, idempotency_key="application:42:decline")

    assert first.status is EmailDeliveryStatus.ACCEPTED
    assert first.provider_message_id == second.provider_message_id
    assert "audit@example.com" in client.recipients
    assert client.messages[0].get("Bcc") is None
    assert client.messages[0].get_payload()[-1].get_filename() == "resume.txt"
    assert client.started_tls is True
    assert client.closed is True


@pytest.mark.asyncio
async def test_email_tool_is_keyed_external_and_returns_provider_receipt() -> None:
    class Sender:
        async def send(self, value: EmailMessage, *, idempotency_key: str) -> EmailDeliveryReceipt:
            assert value.to[0].address == "candidate@example.com"
            return EmailDeliveryReceipt(
                provider="test",
                provider_message_id=idempotency_key,
                status="accepted",
                accepted_recipients=(value.to[0].address,),
            )

    tool = email_tool(Sender())
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry, SimpleNamespace(evaluate=_allow))
    context = ToolContext(
        run_id="run-1",
        step_id="send-1",
        tenant_id="tenant",
        user_id="operator",
        permissions=frozenset({"communications.email.send"}),
        idempotency_key="application:42:decline",
    )

    result = await executor.execute(
        tool.definition.name,
        message().model_dump(mode="json"),
        context,
    )

    assert tool.definition.side_effect.value == "external"
    assert tool.definition.idempotency.value == "keyed"
    assert result.value["provider_message_id"] == "application:42:decline"


@pytest.mark.asyncio
async def test_smtp_failure_is_recorded_as_indeterminate_and_never_replayed() -> None:
    class FailingClient(FakeSmtpClient):
        def send_message(self, value: Any, *, to_addrs: tuple[str, ...]) -> Any:
            raise smtplib.SMTPServerDisconnected("connection lost after DATA")

    sender = SmtpEmailSender(
        SmtpSettings(host="smtp.example.com"),
        smtp_factory=lambda settings: FailingClient(),
    )
    tool = email_tool(sender)
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry, SimpleNamespace(evaluate=_allow))
    context = ToolContext(
        run_id="run-1",
        step_id="send-1",
        tenant_id="tenant",
        user_id="operator",
        permissions=frozenset({"communications.email.send"}),
        idempotency_key="application:42:decline",
    )

    with pytest.raises(ToolExecutionError, match="EmailDeliveryError"):
        await executor.execute(tool.definition.name, message().model_dump(mode="json"), context)
    with pytest.raises(ToolExecutionError, match="manual reconciliation"):
        await executor.execute(tool.definition.name, message().model_dump(mode="json"), context)


async def _allow(point: str, payload: Any, context: Any) -> Any:
    return SimpleNamespace(action="allow", value=payload, reason="ok")


@pytest.mark.asyncio
async def test_smtp_attachment_requires_loader() -> None:
    sender = SmtpEmailSender(
        SmtpSettings(host="localhost", security="plain"),
        smtp_factory=lambda settings: FakeSmtpClient(),
    )
    value = message(
        attachments=[
            {
                "artifact_id": "artifact-1",
                "filename": "resume.pdf",
                "media_type": "application/pdf",
            }
        ]
    )

    with pytest.raises(EmailDeliveryError, match="artifact loader"):
        await sender.send(value, idempotency_key="key")

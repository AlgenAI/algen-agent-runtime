from __future__ import annotations

import asyncio
import hashlib
import re
import smtplib
import ssl
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from email.message import EmailMessage as MimeEmailMessage
from email.utils import formataddr, parseaddr
from enum import StrEnum
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)
from algen_agent_runtime.types.contracts import RetryPolicy, utc_now

_HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{1,126}$")
_DOMAIN = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")


def _reject_header_injection(value: str, field: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError(f"{field} cannot contain line breaks")
    return value


class EmailAddress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    address: str = Field(min_length=3, max_length=320)
    display_name: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_address(self) -> EmailAddress:
        _reject_header_injection(self.address, "email address")
        _, parsed = parseaddr(self.address)
        if parsed != self.address or "@" not in parsed or parsed.startswith("@"):
            raise ValueError("email address is invalid")
        local, _, domain = parsed.rpartition("@")
        if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise ValueError("email address is invalid")
        if self.display_name is not None:
            _reject_header_injection(self.display_name, "display name")
        return self

    def formatted(self) -> str:
        return formataddr((self.display_name or "", self.address))


class EmailAttachmentReference(BaseModel):
    """Tenant-scoped Runtime artifact reference; message contracts never embed file bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: str = Field(min_length=1, max_length=256)
    filename: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=3, max_length=255)

    @model_validator(mode="after")
    def validate_filename(self) -> EmailAttachmentReference:
        _reject_header_injection(self.filename, "attachment filename")
        if "/" in self.filename or "\\" in self.filename or self.filename in {".", ".."}:
            raise ValueError("attachment filename must not contain a path")
        media_type, separator, subtype = self.media_type.partition("/")
        if not separator or not media_type or not subtype:
            raise ValueError("attachment media_type must be a type/subtype value")
        if any(character.isspace() for character in self.media_type) or any(
            character in self.media_type for character in ";\r\n"
        ):
            raise ValueError("attachment media_type must not contain parameters or whitespace")
        return self


class EmailMessage(BaseModel):
    """Provider-neutral outbound message with bounded, header-safe content."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    sender: EmailAddress
    to: tuple[EmailAddress, ...] = Field(min_length=1, max_length=100)
    cc: tuple[EmailAddress, ...] = Field(default=(), max_length=100)
    bcc: tuple[EmailAddress, ...] = Field(default=(), max_length=100)
    reply_to: EmailAddress | None = None
    subject: str = Field(min_length=1, max_length=998)
    text_body: str | None = Field(default=None, max_length=1_000_000)
    html_body: str | None = Field(default=None, max_length=2_000_000)
    attachments: tuple[EmailAttachmentReference, ...] = Field(default=(), max_length=20)
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_message(self) -> EmailMessage:
        _reject_header_injection(self.subject, "email subject")
        if self.text_body is None and self.html_body is None:
            raise ValueError("email requires text_body or html_body")
        recipients = (*self.to, *self.cc, *self.bcc)
        if len(recipients) > 100:
            raise ValueError("email supports at most 100 total recipients")
        normalized = [item.address.casefold() for item in recipients]
        if len(normalized) != len(set(normalized)):
            raise ValueError("email recipients must be unique across to, cc, and bcc")
        if len(self.headers) > 32:
            raise ValueError("email supports at most 32 custom headers")
        for name, value in self.headers.items():
            if not name.lower().startswith("x-"):
                raise ValueError("custom email headers must use the X- prefix")
            _reject_header_injection(name, "email header name")
            _reject_header_injection(value, "email header value")
            if not _HEADER_NAME.fullmatch(name):
                raise ValueError("custom email header name is invalid")
        return self


class EmailDeliveryStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class EmailDeliveryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str
    provider_message_id: str = Field(min_length=1, max_length=998)
    status: EmailDeliveryStatus
    accepted_recipients: tuple[str, ...] = ()
    rejected_recipients: dict[str, str] = Field(default_factory=dict)
    accepted_at: datetime = Field(default_factory=utc_now)


class EmailDeliveryError(RuntimeError):
    """Content-minimal transport failure safe to surface through Runtime tooling."""


class EmailSender(Protocol):
    async def send(
        self, message: EmailMessage, *, idempotency_key: str
    ) -> EmailDeliveryReceipt: ...


class SmtpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=587, ge=1, le=65535)
    security: Literal["starttls", "tls", "plain"] = "starttls"
    username: str | None = Field(default=None, max_length=320)
    password: SecretStr | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    local_hostname: str | None = Field(default=None, max_length=253)
    allow_insecure_plaintext: bool = False
    message_id_domain: str = Field(default="algen.local", min_length=1, max_length=253)
    max_attachment_bytes: int = Field(default=20_000_000, ge=1, le=100_000_000)

    @model_validator(mode="after")
    def validate_transport(self) -> SmtpSettings:
        _reject_header_injection(self.host, "SMTP host")
        _reject_header_injection(self.message_id_domain, "message ID domain")
        if not _DOMAIN.fullmatch(self.message_id_domain):
            raise ValueError("message_id_domain is invalid")
        if (self.username is None) != (self.password is None):
            raise ValueError("SMTP username and password must be configured together")
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if (
            self.security == "plain"
            and self.host.casefold() not in local_hosts
            and not self.allow_insecure_plaintext
        ):
            raise ValueError("plaintext SMTP is restricted to loopback unless explicitly enabled")
        return self


EmailArtifactLoader = Callable[[EmailAttachmentReference], Awaitable[bytes]]
SmtpFactory = Callable[[SmtpSettings], Any]


class SmtpEmailSender:
    """SMTP adapter with deterministic Message-ID and bounded artifact attachments."""

    def __init__(
        self,
        settings: SmtpSettings,
        *,
        artifact_loader: EmailArtifactLoader | None = None,
        smtp_factory: SmtpFactory | None = None,
    ) -> None:
        self._settings = settings
        self._artifact_loader = artifact_loader
        self._smtp_factory = smtp_factory or self._open_client

    async def send(self, message: EmailMessage, *, idempotency_key: str) -> EmailDeliveryReceipt:
        attachments: list[tuple[EmailAttachmentReference, bytes]] = []
        total_bytes = 0
        for reference in message.attachments:
            if self._artifact_loader is None:
                raise EmailDeliveryError("SMTP attachments require an artifact loader")
            payload = await self._artifact_loader(reference)
            total_bytes += len(payload)
            if total_bytes > self._settings.max_attachment_bytes:
                raise EmailDeliveryError("email attachments exceed the configured size limit")
            attachments.append((reference, payload))
        mime, message_id = self._build_message(message, idempotency_key, attachments)
        all_recipients = tuple(item.address for item in (*message.to, *message.cc, *message.bcc))
        try:
            refused = await asyncio.to_thread(self._send_sync, mime, all_recipients)
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            raise EmailDeliveryError(f"SMTP delivery failed ({type(exc).__name__})") from exc
        rejected = {address: f"SMTP {details[0]}" for address, details in refused.items()}
        accepted = tuple(address for address in all_recipients if address not in refused)
        return EmailDeliveryReceipt(
            provider="smtp",
            provider_message_id=message_id,
            status=(EmailDeliveryStatus.ACCEPTED if accepted else EmailDeliveryStatus.REJECTED),
            accepted_recipients=accepted,
            rejected_recipients=rejected,
        )

    def _build_message(
        self,
        message: EmailMessage,
        idempotency_key: str,
        attachments: list[tuple[EmailAttachmentReference, bytes]],
    ) -> tuple[MimeEmailMessage, str]:
        mime = MimeEmailMessage()
        message_id = (
            f"<{hashlib.sha256(idempotency_key.encode()).hexdigest()}@"
            f"{self._settings.message_id_domain}>"
        )
        mime["Message-ID"] = message_id
        mime["From"] = message.sender.formatted()
        mime["To"] = ", ".join(item.formatted() for item in message.to)
        if message.cc:
            mime["Cc"] = ", ".join(item.formatted() for item in message.cc)
        if message.reply_to is not None:
            mime["Reply-To"] = message.reply_to.formatted()
        mime["Subject"] = message.subject
        for name, value in message.headers.items():
            mime[name] = value
        if message.text_body is not None:
            mime.set_content(message.text_body)
        else:
            mime.set_content("This message requires an HTML-capable email client.")
        if message.html_body is not None:
            mime.add_alternative(message.html_body, subtype="html")
        for reference, payload in attachments:
            media_type, _, subtype = reference.media_type.partition("/")
            mime.add_attachment(
                payload,
                maintype=media_type,
                subtype=subtype or "octet-stream",
                filename=reference.filename,
            )
        return mime, message_id

    def _open_client(self, settings: SmtpSettings) -> Any:
        if settings.security == "tls":
            return smtplib.SMTP_SSL(
                settings.host,
                settings.port,
                local_hostname=settings.local_hostname,
                timeout=settings.timeout_seconds,
                context=ssl.create_default_context(),
            )
        return smtplib.SMTP(
            settings.host,
            settings.port,
            local_hostname=settings.local_hostname,
            timeout=settings.timeout_seconds,
        )

    def _send_sync(
        self, message: MimeEmailMessage, recipients: tuple[str, ...]
    ) -> Mapping[str, tuple[int, bytes]]:
        client = self._smtp_factory(self._settings)
        try:
            client.ehlo()
            if self._settings.security == "starttls":
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if self._settings.username is not None and self._settings.password is not None:
                client.login(self._settings.username, self._settings.password.get_secret_value())
            return cast(
                Mapping[str, tuple[int, bytes]],
                client.send_message(message, to_addrs=recipients),
            )
        finally:
            try:
                client.quit()
            except (smtplib.SMTPException, OSError):
                client.close()


def email_tool(sender: EmailSender, *, name: str = "communications.email.send") -> Tool:
    """Build a governed, approval-compatible outbound email Runtime tool."""

    async def execute(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        message = EmailMessage.model_validate(arguments)
        receipt = await sender.send(message, idempotency_key=context.idempotency_key)
        return receipt.model_dump(mode="json")

    return Tool(
        ToolDefinition(
            name=name,
            version="1.0.0",
            description="Send a bounded email through a deployment-configured provider.",
            input_schema=EmailMessage.model_json_schema(),
            output_schema=EmailDeliveryReceipt.model_json_schema(),
            required_permissions=frozenset({"communications.email.send"}),
            side_effect=SideEffect.EXTERNAL,
            idempotency=Idempotency.KEYED,
            timeout_seconds=300,
            retry_policy=RetryPolicy(max_attempts=1),
            max_result_bytes=64_000,
            audit_metadata={"content_capture": "disabled", "channel": "email"},
        ),
        execute,
    )

"""Gmail API mailbox: OAuth refresh-token sending and bounded inbox reads."""

from __future__ import annotations

import asyncio
import base64
import re
import time
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from algen_agent_runtime.communications.email import (
    _DOMAIN,
    EmailArtifactLoader,
    EmailAttachmentReference,
    EmailDeliveryError,
    EmailDeliveryReceipt,
    EmailDeliveryStatus,
    EmailMessage,
    _build_mime_message,
)
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition


class GmailOAuthSettings(BaseModel):
    """OAuth refresh-token settings for one Gmail or Google Workspace mailbox."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    client_id: str = Field(min_length=1, max_length=1_000)
    client_secret: SecretStr
    refresh_token: SecretStr
    token_uri: str = "https://oauth2.googleapis.com/token"
    api_base_url: str = "https://gmail.googleapis.com/gmail/v1"
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    message_id_domain: str = Field(default="algen.local", min_length=1, max_length=253)
    max_attachment_bytes: int = Field(default=20_000_000, ge=1, le=100_000_000)

    @model_validator(mode="after")
    def validate_settings(self) -> GmailOAuthSettings:
        if not _DOMAIN.fullmatch(self.message_id_domain):
            raise ValueError("message_id_domain is invalid")
        return self


class InboundEmailAttachment(BaseModel):
    """Bounded Gmail attachment descriptor; bytes are fetched only on explicit demand."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    provider_attachment_id: str = Field(min_length=1, max_length=2_000)
    filename: str = Field(min_length=1, max_length=255)
    media_type: str = Field(default="application/octet-stream", min_length=3, max_length=255)
    size: int = Field(default=0, ge=0)


class InboundEmailSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider_message_id: str
    thread_id: str | None = None
    sender: str = ""
    recipients: str = ""
    subject: str = ""
    received_at_ms: int = Field(default=0, ge=0)
    snippet: str = Field(default="", max_length=2_000)
    labels: tuple[str, ...] = ()
    attachments: tuple[InboundEmailAttachment, ...] = ()


def _gmail_attachments(payload: Mapping[str, Any]) -> tuple[InboundEmailAttachment, ...]:
    attachments: list[InboundEmailAttachment] = []
    pending: list[Mapping[str, Any]] = [payload]
    while pending:
        part = pending.pop()
        pending.extend(child for child in part.get("parts", ()) if isinstance(child, Mapping))
        body = part.get("body") or {}
        attachment_id = str(body.get("attachmentId") or "")
        filename = re.split(r"[/\\]", str(part.get("filename") or ""))[-1]
        if not attachment_id or not filename:
            continue
        attachments.append(
            InboundEmailAttachment(
                provider_attachment_id=attachment_id,
                filename=filename,
                media_type=str(part.get("mimeType") or "application/octet-stream"),
                size=max(0, int(body.get("size") or 0)),
            )
        )
    return tuple(attachments[:20])


class GmailMailboxPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    messages: tuple[InboundEmailSummary, ...] = ()
    next_page_token: str | None = None
    result_size_estimate: int = Field(default=0, ge=0)


class GmailMailboxClient:
    """Google Gmail API sender and bounded inbox reader using OAuth refresh tokens."""

    def __init__(
        self,
        settings: GmailOAuthSettings,
        *,
        artifact_loader: EmailArtifactLoader | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._artifact_loader = artifact_loader
        self._client = client or httpx.AsyncClient(timeout=settings.timeout_seconds)
        self._owns_client = client is None
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def _token(self) -> str:
        if self._access_token and time.monotonic() < self._access_token_expires_at - 60:
            return self._access_token
        async with self._token_lock:
            if self._access_token and time.monotonic() < self._access_token_expires_at - 60:
                return self._access_token
            try:
                response = await self._client.post(
                    self._settings.token_uri,
                    data={
                        "client_id": self._settings.client_id,
                        "client_secret": self._settings.client_secret.get_secret_value(),
                        "refresh_token": self._settings.refresh_token.get_secret_value(),
                        "grant_type": "refresh_token",
                    },
                )
                response.raise_for_status()
                payload = response.json()
                token = str(payload["access_token"])
                expires_in = max(60, int(payload.get("expires_in", 3600)))
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                raise EmailDeliveryError(
                    f"Gmail OAuth token request failed ({type(exc).__name__})"
                ) from exc
            self._access_token = token
            self._access_token_expires_at = time.monotonic() + expires_in
            return token

    async def send(self, message: EmailMessage, *, idempotency_key: str) -> EmailDeliveryReceipt:
        attachments: list[tuple[EmailAttachmentReference, bytes]] = []
        total_bytes = 0
        for reference in message.attachments:
            if self._artifact_loader is None:
                raise EmailDeliveryError("Gmail attachments require an artifact loader")
            payload = await self._artifact_loader(reference)
            total_bytes += len(payload)
            if total_bytes > self._settings.max_attachment_bytes:
                raise EmailDeliveryError("email attachments exceed the configured size limit")
            attachments.append((reference, payload))
        mime, _ = _build_mime_message(
            message,
            idempotency_key=idempotency_key,
            attachments=attachments,
            message_id_domain=self._settings.message_id_domain,
        )
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode().rstrip("=")
        token = await self._token()
        try:
            response = await self._client.post(
                f"{self._settings.api_base_url.rstrip('/')}/users/me/messages/send",
                headers={"Authorization": f"Bearer {token}"},
                json={"raw": raw},
            )
            response.raise_for_status()
            provider_id = str(response.json()["id"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise EmailDeliveryError(f"Gmail delivery failed ({type(exc).__name__})") from exc
        recipients = tuple(item.address for item in (*message.to, *message.cc, *message.bcc))
        return EmailDeliveryReceipt(
            provider="gmail",
            provider_message_id=provider_id,
            status=EmailDeliveryStatus.ACCEPTED,
            accepted_recipients=recipients,
        )

    async def list_messages(
        self,
        *,
        query: str = "is:unread",
        max_results: int = 25,
        page_token: str | None = None,
    ) -> GmailMailboxPage:
        if not 1 <= max_results <= 100:
            raise ValueError("max_results must be between 1 and 100")
        token = await self._token()
        headers = {"Authorization": f"Bearer {token}"}
        params: dict[str, str | int] = {"q": query, "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        try:
            response = await self._client.get(
                f"{self._settings.api_base_url.rstrip('/')}/users/me/messages",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
            listing = response.json()
            rows: list[InboundEmailSummary] = []
            for reference in listing.get("messages", []):
                detail = await self._client.get(
                    f"{self._settings.api_base_url.rstrip('/')}/users/me/messages/{reference['id']}",
                    headers=headers,
                    params={
                        "format": "full",
                        "metadataHeaders": ["From", "To", "Cc", "Subject", "Date"],
                    },
                )
                detail.raise_for_status()
                payload = detail.json()
                email_headers = {
                    str(item.get("name", "")).casefold(): str(item.get("value", ""))
                    for item in payload.get("payload", {}).get("headers", [])
                }
                rows.append(
                    InboundEmailSummary(
                        provider_message_id=str(payload["id"]),
                        thread_id=str(payload.get("threadId") or "") or None,
                        sender=email_headers.get("from", ""),
                        recipients=email_headers.get("to", ""),
                        subject=email_headers.get("subject", ""),
                        received_at_ms=max(0, int(payload.get("internalDate", 0))),
                        snippet=str(payload.get("snippet", ""))[:2_000],
                        labels=tuple(str(item) for item in payload.get("labelIds", [])),
                        attachments=_gmail_attachments(payload.get("payload", {})),
                    )
                )
            return GmailMailboxPage(
                messages=tuple(rows),
                next_page_token=listing.get("nextPageToken"),
                result_size_estimate=max(0, int(listing.get("resultSizeEstimate", len(rows)))),
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise EmailDeliveryError(f"Gmail inbox read failed ({type(exc).__name__})") from exc

    async def download_attachment(
        self, provider_message_id: str, provider_attachment_id: str
    ) -> bytes:
        if not provider_message_id or len(provider_message_id) > 2_000:
            raise ValueError("provider_message_id is invalid")
        if not provider_attachment_id or len(provider_attachment_id) > 2_000:
            raise ValueError("provider_attachment_id is invalid")
        token = await self._token()
        try:
            response = await self._client.get(
                f"{self._settings.api_base_url.rstrip('/')}/users/me/messages/"
                f"{provider_message_id}/attachments/{provider_attachment_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            encoded = str(response.json()["data"])
            payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            if len(payload) > self._settings.max_attachment_bytes:
                raise EmailDeliveryError("Gmail attachment exceeds the configured size limit")
            return payload
        except EmailDeliveryError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise EmailDeliveryError(
                f"Gmail attachment download failed ({type(exc).__name__})"
            ) from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def gmail_inbox_tool(
    mailbox: GmailMailboxClient, *, name: str = "communications.email.inbox"
) -> Tool:
    async def execute(arguments: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        page = await mailbox.list_messages(
            query=str(arguments.get("query") or "is:unread"),
            max_results=int(arguments.get("max_results") or 25),
            page_token=str(arguments.get("page_token") or "") or None,
        )
        return page.model_dump(mode="json")

    return Tool(
        ToolDefinition(
            name=name,
            version="1.0.0",
            description="List bounded Gmail message metadata through a deployment-configured mailbox.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "maxLength": 500},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
                    "page_token": {"type": "string", "maxLength": 2_000},
                },
                "additionalProperties": False,
            },
            output_schema=GmailMailboxPage.model_json_schema(),
            required_permissions=frozenset({"communications.email.read"}),
            side_effect=SideEffect.READ,
            timeout_seconds=300,
            max_result_bytes=256_000,
            audit_metadata={"content_capture": "disabled", "channel": "email"},
        ),
        execute,
    )

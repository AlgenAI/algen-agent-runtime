# Email tool and SMTP adapter

> [!WARNING]
> **Deprecated.** `algen_agent_runtime.communications` moved to the
> [algen-agent-tools](https://github.com/AlgenAI/algen-agent-tools) package and warns on import;
> it will be removed in 0.2. Use `algen_agent_tools.mail` for the message contracts and
> `smtp_toolkit` or `gmail_toolkit` (plugins `algen_agent_tools.smtp`, `algen_agent_tools.gmail`)
> for the tools. The new `send_email` tools fix the sender in deployment configuration and do not
> accept custom headers from the model. See its `docs/migrating-from-runtime.md`.

Algen Agent Runtime provides provider-neutral outbound email contracts, a governed Runtime tool, and
a first SMTP adapter. Applications own templates, recipient policy, consent/suppression rules,
provider credentials, inbound mail, and delivery-event reconciliation.

## Contracts and safety defaults

`EmailMessage` requires validated sender and recipient addresses, a bounded subject, and text or HTML
content. It caps total recipients, body sizes, attachments, and custom headers; rejects header
injection, duplicate recipients, filesystem paths, and non-`X-` custom headers; and represents every
attachment as a Runtime artifact ID. It never accepts embedded attachment bytes or local paths.

`email_tool(sender)` creates `communications.email.send`, classified as an `external`, keyed side
effect requiring `communications.email.send`. It performs one provider attempt. Runtime's durable
tool-execution ledger replays a completed receipt for the same idempotency key and marks any failed or
interrupted send indeterminate, requiring reconciliation rather than an automatic duplicate send.
Place the tool behind an approval policy or a first-class workflow approval node whenever a human
must own the outbound message.

## SMTP adapter

```python
from algen_agent_runtime.communications import SmtpEmailSender, SmtpSettings, email_tool

sender = SmtpEmailSender(
    SmtpSettings(
        host="smtp.example.com",
        port=587,
        security="starttls",
        username="service-account",
        password="resolved-by-the-host-secret-manager",
        message_id_domain="mail.example.com",
    ),
    artifact_loader=load_available_tenant_artifact,
)
container.tools.register(email_tool(sender))
```

The adapter uses STARTTLS by default. Implicit TLS is supported; plaintext SMTP is restricted to
loopback unless explicitly enabled. A stable `Message-ID` is derived from Runtime's idempotency key,
BCC recipients are passed only to the SMTP envelope, credentials are never returned, and receipts
contain delivery metadata rather than message bodies. The artifact loader must enforce tenant scope,
`available` lifecycle state, media policy, and authorization before returning bytes.

SMTP acceptance is not proof of inbox delivery. A timeout or disconnect may occur after the server
accepted DATA, so the Runtime tool ledger treats the outcome as indeterminate. An operator or
application reconciliation process must query the provider or inspect delivery events before retrying.

## Gmail

The Gmail adapter (`GmailMailboxClient`, `gmail_inbox_tool`) was removed in 0.1.0a12. Use
`gmail_toolkit` from [algen-agent-tools](https://github.com/AlgenAI/algen-agent-tools)
(`pip install "algen-agent-tools[gmail]"`, plugin `algen_agent_tools.gmail`), which provides
`gmail.list_messages` and `gmail.send_email`.

## Application boundary

Microsoft Graph/Outlook, Amazon SES, SendGrid, Resend, inbound mailbox ingestion, webhook
signature verification, polling schedules, bounce/complaint handling, suppression lists, scheduled sending, and reply
threading are connector implementations above the same `EmailSender` contract. They should preserve
Runtime idempotency keys, return `EmailDeliveryReceipt`, and keep provider SDKs optional.

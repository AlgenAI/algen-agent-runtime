# Email

The Runtime has no email connector. `algen_agent_runtime.communications` (the message contracts,
`SmtpEmailSender` and `email_tool`) was removed in 0.2, after warning since 0.1.0a12. The Gmail
adapter was removed in 0.1.0a12.

Use the [algen-agent-tools](https://github.com/AlgenAI/algen-agent-tools) package instead:

| Removed from the Runtime | Use |
| --- | --- |
| `communications.EmailMessage` and the other message contracts | `algen_agent_tools.mail` |
| `SmtpEmailSender`, `SmtpSettings`, `email_tool` | `smtp_toolkit` (plugin `algen_agent_tools.smtp`) |
| `GmailMailboxClient`, `gmail_inbox_tool` | `gmail_toolkit` (plugin `algen_agent_tools.gmail`) |

The `send_email` tools there fix the sender in deployment configuration and do not accept custom
headers from the model. Its `docs/migrating-from-runtime.md` covers the move.

## Running a send tool safely

Whatever sends the mail, the Runtime governs the call. Declare the tool as an `external`, keyed side
effect. The tool-execution ledger then replays a completed receipt for the same idempotency key and
marks a failed or interrupted send indeterminate, so it is reconciled rather than sent twice. Place
the tool behind an approval policy or a workflow approval node whenever a person must own the
outbound message.

SMTP acceptance is not proof of inbox delivery. A timeout or disconnect may occur after the server
accepted the message, so treat the outcome as indeterminate until the provider or its delivery
events confirm it.

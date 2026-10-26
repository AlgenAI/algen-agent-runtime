"""Deprecated: outbound email moved to the ``algen-agent-tools`` package.

Import from ``algen_agent_tools.mail`` (message contracts), ``algen_agent_tools.smtp`` and
``algen_agent_tools.gmail`` instead; ``smtp_toolkit`` and ``gmail_toolkit`` replace
``email_tool`` and ``gmail_inbox_tool``. This package keeps working, with a
``DeprecationWarning`` on import, until it is removed in 0.2.
"""

import importlib
import os
import warnings

from algen_agent_runtime.communications.email import (
    EmailAddress,
    EmailAttachmentReference,
    EmailDeliveryError,
    EmailDeliveryReceipt,
    EmailDeliveryStatus,
    EmailMessage,
    EmailSender,
    SmtpEmailSender,
    SmtpSettings,
    email_tool,
)
from algen_agent_runtime.communications.gmail import (
    GmailMailboxClient,
    GmailMailboxPage,
    GmailOAuthSettings,
    InboundEmailAttachment,
    InboundEmailSummary,
    gmail_inbox_tool,
)

__all__ = [
    "EmailAddress",
    "EmailAttachmentReference",
    "EmailDeliveryError",
    "EmailDeliveryReceipt",
    "EmailDeliveryStatus",
    "EmailMessage",
    "EmailSender",
    "GmailMailboxClient",
    "GmailMailboxPage",
    "GmailOAuthSettings",
    "InboundEmailAttachment",
    "InboundEmailSummary",
    "SmtpEmailSender",
    "SmtpSettings",
    "email_tool",
    "gmail_inbox_tool",
]

warnings.warn(
    "algen_agent_runtime.communications is deprecated and will be removed in 0.2: install "
    "algen-agent-tools and import from algen_agent_tools.mail, algen_agent_tools.smtp and "
    "algen_agent_tools.gmail (see its docs/migrating-from-runtime.md)",
    DeprecationWarning,
    # Attribute the warning to the module that imported us, not to the import machinery.
    skip_file_prefixes=(
        "<frozen importlib",
        os.path.dirname(importlib.__file__),
        os.path.dirname(__file__),
    ),
)

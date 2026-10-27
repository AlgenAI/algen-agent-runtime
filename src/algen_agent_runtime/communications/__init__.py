"""Deprecated: outbound email moved to the ``algen-agent-tools`` package.

Import from ``algen_agent_tools.mail`` (message contracts) and ``algen_agent_tools.smtp``
instead; ``smtp_toolkit`` replaces ``email_tool``. This package keeps working, with a
``DeprecationWarning`` on import, until it is removed in 0.2.

The Gmail adapter (``GmailMailboxClient``, ``gmail_inbox_tool``) was removed in 0.1.0a12:
use ``gmail_toolkit`` from ``algen_agent_tools.gmail``.
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

__all__ = [
    "EmailAddress",
    "EmailAttachmentReference",
    "EmailDeliveryError",
    "EmailDeliveryReceipt",
    "EmailDeliveryStatus",
    "EmailMessage",
    "EmailSender",
    "SmtpEmailSender",
    "SmtpSettings",
    "email_tool",
]

warnings.warn(
    "algen_agent_runtime.communications is deprecated and will be removed in 0.2: install "
    "algen-agent-tools and import from algen_agent_tools.mail and algen_agent_tools.smtp "
    "(see its docs/migrating-from-runtime.md)",
    DeprecationWarning,
    # Attribute the warning to the module that imported us, not to the import machinery.
    skip_file_prefixes=(
        "<frozen importlib",
        os.path.dirname(importlib.__file__),
        os.path.dirname(__file__),
    ),
)

_REMOVED = frozenset(
    {
        "GmailMailboxClient",
        "GmailMailboxPage",
        "GmailOAuthSettings",
        "InboundEmailAttachment",
        "InboundEmailSummary",
        "gmail_inbox_tool",
    }
)


def __getattr__(name: str) -> object:
    if name in _REMOVED:
        raise ImportError(
            f"algen_agent_runtime.communications.{name} was removed in 0.1.0a12: install "
            '"algen-agent-tools[gmail]" and use algen_agent_tools.gmail.gmail_toolkit'
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

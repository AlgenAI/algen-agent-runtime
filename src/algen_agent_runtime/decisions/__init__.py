"""Experimental: the typed client for the TypeSafe Jev decision service.

Outside the compatibility promise (``docs/api-stability.md``, "Experimental interfaces"): it is
tested against a mocked HTTP API only, and no application uses it yet.
"""

from algen_agent_runtime.decisions.typesafe import (
    JevAnswer,
    JevDecisionClient,
    JevDecisionError,
    JevQuestion,
    JevRequest,
    JevResponse,
    JevSettings,
    JevUsage,
)

#: Marks this package experimental (``algen_agent_runtime.stability``).
__experimental__ = (
    "a client for a third-party decision service, tested against a mocked HTTP API only and not "
    "used by an application yet"
)

__all__ = [
    "JevAnswer",
    "JevDecisionClient",
    "JevDecisionError",
    "JevQuestion",
    "JevRequest",
    "JevResponse",
    "JevSettings",
    "JevUsage",
]

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

SENSITIVE_KEYS = frozenset(
    {"authorization", "cookie", "password", "secret", "token", "api_key", "apikey", "credential"}
)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+=*")
# The top-level keys an approval request's parameters are redacted on (see redact_parameters).
APPROVAL_SENSITIVE_KEYS = frozenset({"password", "secret", "token", "api_key", "authorization"})


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return _BEARER.sub("Bearer [REDACTED]", value)
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]" if str(key).lower() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    return value


def redact_parameters(value: Mapping[str, Any]) -> dict[str, Any]:
    """Tool arguments as an approval request shows them: top-level keys named in
    ``APPROVAL_SENSITIVE_KEYS`` (case-insensitive) become ``"[REDACTED]"``.

    The runtime stores an approval's ``redacted_parameters`` through this, and the approval
    routes return ``modified_parameters`` through it, so both read the same way.
    """
    return {
        key: "[REDACTED]" if key.lower() in APPROVAL_SENSITIVE_KEYS else item
        for key, item in value.items()
    }

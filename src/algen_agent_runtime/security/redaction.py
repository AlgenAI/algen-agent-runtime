from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

SENSITIVE_KEYS = frozenset(
    {"authorization", "cookie", "password", "secret", "token", "api_key", "apikey", "credential"}
)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+=*")
# ``api_key=...``, ``password: ...``: the same assignments the ``secrets`` policy redacts.
_ASSIGNMENT = re.compile(
    r"(?i)(?:api[_-]?key|password|secret|token)\s*[:=]\s*([A-Za-z0-9_\-/.]{8,})"
)
# The top-level keys an approval request's parameters are redacted on (see redact_parameters).
APPROVAL_SENSITIVE_KEYS = frozenset({"password", "secret", "token", "api_key", "authorization"})


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]" if str(key).lower() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    return value


def redact_text(value: str) -> str:
    """Bearer tokens and ``key=value`` credential assignments in free text."""
    value = _BEARER.sub("Bearer [REDACTED]", value)
    return _ASSIGNMENT.sub(
        lambda match: match.group(0).replace(match.group(1), "[REDACTED]"), value
    )


def _redact_strings(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {key: _redact_strings(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_redact_strings(item) for item in value]
    return value


def redact_parameters(value: Mapping[str, Any]) -> dict[str, Any]:
    """Tool arguments as an approval request shows them: top-level keys named in
    ``APPROVAL_SENSITIVE_KEYS`` (case-insensitive) become ``"[REDACTED]"``, and bearer tokens
    and ``key=value`` credentials inside any string value are redacted.

    The runtime stores an approval's ``redacted_parameters`` through this, and the approval
    routes return ``modified_parameters`` through it, so both read the same way.
    """
    return {
        key: "[REDACTED]" if key.lower() in APPROVAL_SENSITIVE_KEYS else _redact_strings(item)
        for key, item in value.items()
    }

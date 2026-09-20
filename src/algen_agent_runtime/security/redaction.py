from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

SENSITIVE_KEYS = frozenset(
    {"authorization", "cookie", "password", "secret", "token", "api_key", "apikey", "credential"}
)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+=*")


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

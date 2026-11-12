from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from enum import Enum
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


# The kinds of identifier redact_pii finds, as guardrail_policy.pii.exempt_entities names them.
PII_ENTITIES = ("email", "phone", "aadhaar", "card")


def redact_pii(
    value: Any,
    *,
    entities: Iterable[str] | None = None,
    exempt: Iterable[str] = (),
    schema: Mapping[str, Any] | None = None,
) -> Any:
    """``value`` with personal identifiers masked as the ``pii`` guardrail masks them.

    ``value`` is a string, or a dict, list or tuple of them (nested at any depth; a Pydantic
    model works too); other values are returned as they are, and the input is never modified.
    The rules are the guardrail's own (see docs/agent-tooling.md#what-the-pii-guardrail-redacts):
    ``[REDACTED_EMAIL]``, ``[REDACTED_PHONE]`` (a ten-digit Indian mobile number written as one,
    or with a word such as "phone" before it or in its field's name, such as ``mobile_no``),
    ``[REDACTED_AADHAAR]`` (Verhoeff-checked) and ``[REDACTED_CARD]`` (network prefix and Luhn).

    ``entities`` limits the kinds redacted (default: all of ``PII_ENTITIES``); ``exempt`` leaves
    kinds alone, as ``guardrail_policy.pii.exempt_entities`` does. With ``schema``, the JSON
    Schema ``value`` follows (a tool's input or output schema), fields declaring
    ``"format": "identifier"`` or ``"x-pii": false`` keep a single-token value and fields
    declaring ``"format": "email"`` keep a value that is exactly one address.

    Raises ``ValueError`` for an unknown kind.
    """
    from algen_agent_runtime.policies.engine import PIIRedactionPolicy, _SchemaNode

    selected = set(PII_ENTITIES if entities is None else entities)
    skipped = set(exempt)
    unknown = (selected | skipped) - set(PII_ENTITIES)
    if unknown:
        raise ValueError(
            f"unknown PII kind(s) {sorted(unknown)}; expected any of {list(PII_ENTITIES)}"
        )
    off = frozenset(kind for kind in PII_ENTITIES if kind not in selected or kind in skipped)
    node = _SchemaNode.root(schema) if schema is not None else None
    return PIIRedactionPolicy()._redact(value, node, None, off)[0]


def redact_secrets(value: Any) -> Any:
    """``value`` with credentials masked as ``[REDACTED]``, as the ``secrets`` guardrail does.

    ``value`` is a string, or a dict, list or tuple of them at any depth; the input is never
    modified. In text, the value of a credential written as code or configuration
    (``api_key=...``, ``"password": "..."``, ``SECRET: ...``) and bearer tokens are masked, by
    the guardrail's rules (docs/agent-tooling.md#what-the-pii-guardrail-redacts); "The secret:
    consistency" is left alone. In a dict, the whole value of a key that names a credential
    (``password``, ``api_key``, ``db.password``, ``githubToken``, ``authorization``, ``cookie``;
    not ``max_tokens``) is replaced, whatever it holds.
    """
    from algen_agent_runtime.policies.engine import SecretRedactionPolicy

    def walk(item: Any) -> Any:
        if isinstance(item, Enum):
            return item
        if isinstance(item, str):
            return SecretRedactionPolicy._redact_text(item)[0]
        if isinstance(item, Mapping):
            return {
                key: "[REDACTED]" if _names_credential(key) else walk(child)
                for key, child in item.items()
            }
        if isinstance(item, (list, tuple)):
            items = [walk(child) for child in item]
            return tuple(items) if isinstance(item, tuple) else items
        return item

    return walk(value)


def _names_credential(key: Any) -> bool:
    from algen_agent_runtime.policies.engine import _SECRET_KEY

    if not isinstance(key, str):
        return False
    return (
        key.lower() in SENSITIVE_KEYS or re.fullmatch(_SECRET_KEY, key, re.IGNORECASE) is not None
    )

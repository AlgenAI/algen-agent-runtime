"""Security helpers a host can reuse: the masking the Runtime's guardrails apply.

``redact_pii`` and ``redact_secrets`` apply the ``pii`` and ``secrets`` guardrails' rules to a
string, dict or list, so a host masks logs, exports or its own screens exactly as the Runtime
does (see docs/agent-tooling.md#reusing-the-redaction-rules). The lower-level rules they share
(since 0.2.0b4): ``is_sensitive_key`` (does a key name a credential), ``redact_secret_text`` (mask
token shapes, quoted values, DSN passwords and PEM keys in text), ``SECRET_TOKEN_PATTERNS``,
``hidden_value_marker`` (what an approver sees instead of a hidden value),
``undeclared_sensitive_parameters`` (credential-named arguments a tool's schema does not declare)
and ``fold_text`` (the Unicode folding the detectors match on).
"""

from algen_agent_runtime.security.redaction import (
    PII_ENTITIES,
    SECRET_TOKEN_PATTERNS,
    fold_text,
    hidden_value_marker,
    is_sensitive_key,
    redact_pii,
    redact_secret_text,
    redact_secrets,
    undeclared_sensitive_parameters,
)

__all__ = [
    "PII_ENTITIES",
    "SECRET_TOKEN_PATTERNS",
    "fold_text",
    "hidden_value_marker",
    "is_sensitive_key",
    "redact_pii",
    "redact_secret_text",
    "redact_secrets",
    "undeclared_sensitive_parameters",
]

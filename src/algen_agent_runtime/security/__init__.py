"""Security helpers a host can reuse: the masking the Runtime's guardrails apply.

``redact_pii`` and ``redact_secrets`` apply the ``pii`` and ``secrets`` guardrails' rules to a
string, dict or list, so a host masks logs, exports or its own screens exactly as the Runtime
does (see docs/agent-tooling.md#reusing-the-redaction-rules).
"""

from algen_agent_runtime.security.redaction import PII_ENTITIES, redact_pii, redact_secrets

__all__ = ["PII_ENTITIES", "redact_pii", "redact_secrets"]

"""Shared authentication types: the verified identity and the typed failure."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: Stable, low-cardinality failure reasons used in audit records, logs and metrics.
AuthFailureReason = Literal[
    "missing_credentials",
    "malformed_token",
    "algorithm_not_allowed",
    "unknown_issuer",
    "missing_key_id",
    "unknown_key",
    "keys_unavailable",
    "invalid_signature",
    "expired",
    "not_yet_valid",
    "invalid_issued_at",
    "invalid_audience",
    "invalid_issuer",
    "missing_claim",
    "token_lifetime_exceeded",
    "missing_subject",
    "missing_tenant",
    "tenant_not_allowed",
    "missing_jti",
    "revoked",
    "revocation_unavailable",
    "untrusted_proxy",
    "invalid_service_identity",
    "unknown_service_identity",
    "invalid_token",
]

AuthMethod = Literal["jwt", "oidc", "trusted_proxy"]

_MAX_AUDIT_VALUE = 256


def bounded(value: Any) -> str | None:
    """A short string for audit metadata, or None. Values may come from unverified input."""
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    return text[:_MAX_AUDIT_VALUE]


@dataclass(frozen=True)
class AuthenticatedIdentity:
    """A verified caller, before it becomes an API ``Principal``."""

    tenant_id: str
    user_id: str
    scopes: frozenset[str]
    method: AuthMethod
    issuer: str | None = None
    key_id: str | None = None
    claims: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


class AuthenticationFailure(Exception):
    """Authentication was refused. Carries only content-minimal audit context, never a token.

    ``detail`` is what the client sees; ``reason`` is recorded in audits, logs and metrics.
    ``tenant_id`` is set only when it comes from a verified token.
    """

    def __init__(
        self,
        reason: AuthFailureReason,
        *,
        method: AuthMethod,
        detail: str = "invalid bearer token",
        status_code: int = 401,
        issuer: Any = None,
        key_id: Any = None,
        subject: Any = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason: AuthFailureReason = reason
        self.method: AuthMethod = method
        self.detail = detail
        self.status_code = status_code
        self.issuer = bounded(issuer)
        self.key_id = bounded(key_id)
        self.subject = bounded(subject)
        self.tenant_id = tenant_id

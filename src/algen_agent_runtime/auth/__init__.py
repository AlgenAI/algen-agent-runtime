"""Verified API authentication: OIDC/JWKS, claim mapping, revocation, service identity, audit.

See docs/operations.md#api-authentication and docs/threat-model.md.
"""

from algen_agent_runtime.auth.audit import (
    AUTH_AUDIT_ACTION,
    AUTH_AUDIT_SUPPRESSED_ACTION,
    AUTH_METRIC_NAME,
    AUTHENTICATION_AUDIT_TENANT,
    AuthenticationAuditor,
)
from algen_agent_runtime.auth.authenticator import ApiAuthenticator
from algen_agent_runtime.auth.contracts import (
    AuthenticatedIdentity,
    AuthenticationFailure,
    AuthFailureReason,
)
from algen_agent_runtime.auth.jwks import JsonWebKey, JwksCache, JwksError
from algen_agent_runtime.auth.oidc import OidcVerifier, map_claims
from algen_agent_runtime.auth.revocation import (
    InMemoryTokenRevocationStore,
    PostgresTokenRevocationStore,
    RevocationState,
    TokenRevocationStore,
)
from algen_agent_runtime.auth.service_identity import TrustedProxyIdentity

__all__ = [
    "AUTHENTICATION_AUDIT_TENANT",
    "AUTH_AUDIT_ACTION",
    "AUTH_AUDIT_SUPPRESSED_ACTION",
    "AUTH_METRIC_NAME",
    "ApiAuthenticator",
    "AuthFailureReason",
    "AuthenticatedIdentity",
    "AuthenticationAuditor",
    "AuthenticationFailure",
    "InMemoryTokenRevocationStore",
    "JsonWebKey",
    "JwksCache",
    "JwksError",
    "OidcVerifier",
    "PostgresTokenRevocationStore",
    "RevocationState",
    "TokenRevocationStore",
    "TrustedProxyIdentity",
    "map_claims",
]

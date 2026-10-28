"""Bearer JWT verification against OIDC issuers' JWKS, and claim-to-principal mapping."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from algen_agent_runtime.auth.contracts import (
    AuthenticatedIdentity,
    AuthenticationFailure,
    AuthFailureReason,
)
from algen_agent_runtime.auth.jwks import (
    JwksCache,
    KeysUnavailableError,
    UnknownKeyError,
    algorithm_matches_key,
)
from algen_agent_runtime.config.authentication import (
    OidcClaimMappingSettings,
    OidcIssuerSettings,
    OidcSettings,
)
from algen_agent_runtime.exceptions.errors import ConfigurationError


def claim_value(claims: dict[str, Any], name: str) -> Any:
    """A claim by literal name, else by dotted path into nested objects."""
    if name in claims:
        return claims[name]
    current: Any = claims
    for part in name.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _values(raw: Any) -> Iterable[str]:
    if isinstance(raw, str):
        return raw.split()
    if isinstance(raw, list):
        return (item for item in raw if isinstance(item, str) and item)
    return ()


def map_claims(
    claims: dict[str, Any], mapping: OidcClaimMappingSettings
) -> tuple[str, str, frozenset[str]]:
    """Return ``(tenant_id, user_id, scopes)``; fail closed when tenant or subject is missing."""
    subject = claim_value(claims, mapping.subject_claim)
    if not isinstance(subject, str) or not subject.strip():
        raise AuthenticationFailure("missing_subject", method="oidc")
    subject = subject.strip()
    if mapping.fixed_tenant:
        tenant: Any = mapping.fixed_tenant
    else:
        tenant = claim_value(claims, mapping.tenant_claim or "")
    if not isinstance(tenant, str) or not tenant.strip():
        raise AuthenticationFailure("missing_tenant", method="oidc", subject=subject)
    tenant = tenant.strip()
    if mapping.allowed_tenants and tenant not in mapping.allowed_tenants:
        raise AuthenticationFailure("tenant_not_allowed", method="oidc", subject=subject)
    scopes: set[str] = set()
    for claim in mapping.scope_claims:
        scopes.update(value for value in _values(claim_value(claims, claim)) if value != "*")
    for claim in mapping.role_claims:
        for role in _values(claim_value(claims, claim)):
            scopes.update(mapping.role_scopes.get(role, ()))
    return tenant, subject, frozenset(scopes)


def decode_failure_reason(jwt: Any, exc: Exception) -> AuthFailureReason:
    ordered: tuple[tuple[str, AuthFailureReason], ...] = (
        ("ExpiredSignatureError", "expired"),
        ("ImmatureSignatureError", "not_yet_valid"),
        ("InvalidAudienceError", "invalid_audience"),
        ("InvalidIssuerError", "invalid_issuer"),
        ("MissingRequiredClaimError", "missing_claim"),
        ("InvalidIssuedAtError", "invalid_issued_at"),
        ("InvalidSignatureError", "invalid_signature"),
        ("InvalidAlgorithmError", "algorithm_not_allowed"),
        ("DecodeError", "malformed_token"),
    )
    for name, reason in ordered:
        error_type = getattr(jwt, name, None)
        if isinstance(error_type, type) and isinstance(exc, error_type):
            return reason
    return "invalid_token"


class OidcVerifier:
    """Verifies bearer JWTs from configured issuers. One ``JwksCache`` per issuer."""

    def __init__(
        self,
        settings: OidcSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        try:
            import jwt
        except ImportError as exc:
            raise ConfigurationError(
                "install algen-agent-runtime[auth] for OIDC authentication"
            ) from exc
        self._jwt = jwt
        self._issuers: dict[str, tuple[OidcIssuerSettings, JwksCache]] = {
            issuer.issuer: (
                issuer,
                JwksCache(
                    issuer.issuer,
                    jwks_url=issuer.jwks_url,
                    additional_allowed_hosts=issuer.additional_allowed_hosts,
                    allow_private_networks=settings.allow_private_networks,
                    allow_insecure_http=settings.allow_insecure_http,
                    ttl_seconds=settings.jwks_cache_ttl_seconds,
                    min_refresh_interval_seconds=settings.jwks_min_refresh_interval_seconds,
                    timeout_seconds=settings.http_timeout_seconds,
                    max_bytes=settings.max_jwks_bytes,
                    transport=transport,
                    clock=clock,
                ),
            )
            for issuer in settings.issuers
        }

    def jwks(self, issuer: str) -> JwksCache:
        return self._issuers[issuer][1]

    async def verify(self, token: str) -> AuthenticatedIdentity:
        jwt = self._jwt
        try:
            header = jwt.get_unverified_header(token)
            # Read without verification only to route the token to its issuer's keys.
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise AuthenticationFailure("malformed_token", method="oidc") from exc
        if not isinstance(unverified, dict):
            raise AuthenticationFailure("malformed_token", method="oidc")
        issuer_name = unverified.get("iss")
        kid = header.get("kid")
        algorithm = header.get("alg")
        entry = self._issuers.get(issuer_name) if isinstance(issuer_name, str) else None
        if entry is None:
            raise AuthenticationFailure(
                "unknown_issuer", method="oidc", issuer=issuer_name, key_id=kid
            )
        issuer, cache = entry
        if not isinstance(algorithm, str) or algorithm not in issuer.algorithms:
            raise AuthenticationFailure(
                "algorithm_not_allowed", method="oidc", issuer=issuer.issuer, key_id=kid
            )
        if not isinstance(kid, str) or not kid:
            raise AuthenticationFailure("missing_key_id", method="oidc", issuer=issuer.issuer)
        try:
            key = await cache.get_key(kid)
        except UnknownKeyError as exc:
            raise AuthenticationFailure(
                "unknown_key", method="oidc", issuer=issuer.issuer, key_id=kid
            ) from exc
        except KeysUnavailableError as exc:
            raise AuthenticationFailure(
                "keys_unavailable",
                method="oidc",
                issuer=issuer.issuer,
                key_id=kid,
                status_code=503,
                detail="authentication keys are unavailable",
            ) from exc
        if not algorithm_matches_key(algorithm, key):
            raise AuthenticationFailure(
                "algorithm_not_allowed", method="oidc", issuer=issuer.issuer, key_id=kid
            )
        required = ["exp", "iss", "aud", *issuer.required_claims]
        if issuer.claims.subject_claim == "sub":
            required.append("sub")
        if issuer.max_token_lifetime_seconds is not None:
            required.append("iat")
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[algorithm],
                audience=list(issuer.audiences),
                issuer=issuer.issuer,
                leeway=issuer.leeway_seconds,
                options={"require": list(dict.fromkeys(required))},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationFailure(
                decode_failure_reason(jwt, exc), method="oidc", issuer=issuer.issuer, key_id=kid
            ) from exc
        if issuer.max_token_lifetime_seconds is not None:
            issued_at, expires = claims.get("iat"), claims.get("exp")
            if (
                not isinstance(issued_at, int | float)
                or not isinstance(expires, int | float)
                or expires - issued_at > issuer.max_token_lifetime_seconds
            ):
                raise AuthenticationFailure(
                    "token_lifetime_exceeded",
                    method="oidc",
                    issuer=issuer.issuer,
                    key_id=kid,
                    subject=claims.get("sub"),
                )
        try:
            tenant_id, user_id, scopes = map_claims(claims, issuer.claims)
        except AuthenticationFailure as failure:
            failure.issuer = issuer.issuer
            failure.key_id = kid[:256]
            raise
        return AuthenticatedIdentity(
            tenant_id=tenant_id,
            user_id=user_id,
            scopes=scopes,
            method="oidc",
            issuer=issuer.issuer,
            key_id=kid,
            claims=claims,
        )

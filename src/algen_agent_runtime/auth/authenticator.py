"""Verified API authentication: static-key JWT, OIDC/JWKS, trusted-proxy service identity,
optional revocation, and authentication audit. Framework-neutral; the FastAPI dependency in
``api/dependencies.py`` wraps it."""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from algen_agent_runtime.auth.audit import AuthenticationAuditor
from algen_agent_runtime.auth.contracts import AuthenticatedIdentity, AuthenticationFailure
from algen_agent_runtime.auth.oidc import OidcVerifier, decode_failure_reason
from algen_agent_runtime.auth.revocation import TokenRevocationStore
from algen_agent_runtime.auth.service_identity import TrustedProxyIdentity
from algen_agent_runtime.config.settings import ApiSettings
from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.types.interfaces import AuditLog


class _StaticJwtVerifier:
    """``auth_mode: jwt``: one ``env://`` verification key, unchanged from earlier releases."""

    def __init__(self, settings: ApiSettings) -> None:
        try:
            import jwt
        except ImportError as exc:
            raise ConfigurationError(
                "install algen-agent-runtime[auth] for JWT authentication"
            ) from exc
        key_name = (settings.jwt_key or "").removeprefix("env://")
        key = os.getenv(key_name)
        if not key:
            raise ConfigurationError(
                f"JWT verification key environment variable {key_name!r} is not set"
            )
        self._jwt = jwt
        self._key = key
        self._settings = settings

    def _key_id(self, token: str) -> Any:
        header_reader = getattr(self._jwt, "get_unverified_header", None)
        if header_reader is None:
            return None
        try:
            header = header_reader(token)
        except Exception:
            return None
        return header.get("kid") if isinstance(header, dict) else None

    async def verify(self, token: str) -> AuthenticatedIdentity:
        settings = self._settings
        jwt = self._jwt
        options: dict[str, Any] = {
            "algorithms": list(settings.jwt_algorithms),
            "options": {"require": ["exp", "sub", settings.tenant_claim]},
        }
        if settings.jwt_issuer:
            options["issuer"] = settings.jwt_issuer
        if settings.jwt_audience:
            options["audience"] = settings.jwt_audience
        try:
            claims = jwt.decode(token, self._key, **options)
        except jwt.PyJWTError as exc:
            raise AuthenticationFailure(
                decode_failure_reason(jwt, exc),
                method="jwt",
                issuer=settings.jwt_issuer,
                key_id=self._key_id(token),
            ) from exc
        tenant_id = str(claims.get(settings.tenant_claim, "")).strip()
        user_id = str(claims.get("sub", "")).strip()
        raw_scopes = claims.get(settings.scopes_claim, "")
        scopes = (
            frozenset(str(item) for item in raw_scopes)
            if isinstance(raw_scopes, list)
            else frozenset(str(raw_scopes).split())
        )
        if not tenant_id or not user_id:
            raise AuthenticationFailure(
                "missing_tenant" if not tenant_id else "missing_subject",
                method="jwt",
                detail="token identity claims are incomplete",
                issuer=settings.jwt_issuer,
                subject=user_id or None,
            )
        return AuthenticatedIdentity(
            tenant_id=tenant_id,
            user_id=user_id,
            scopes=scopes,
            method="jwt",
            issuer=settings.jwt_issuer,
            claims=claims if isinstance(claims, dict) else {},
        )


class ApiAuthenticator:
    """Authenticates one request from its headers and direct peer address."""

    def __init__(
        self,
        settings: ApiSettings,
        *,
        audits: AuditLog | None = None,
        revocations: TokenRevocationStore | None = None,
        oidc_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if settings.auth_mode == "development_headers":
            raise ConfigurationError("ApiAuthenticator verifies jwt or oidc authentication only")
        self._verifier: _StaticJwtVerifier | OidcVerifier = (
            OidcVerifier(settings.oidc, transport=oidc_transport)
            if settings.auth_mode == "oidc"
            else _StaticJwtVerifier(settings)
        )
        self._method: Any = settings.auth_mode
        self.auditor = AuthenticationAuditor(audits, settings.authentication_audit)
        self.proxy = (
            TrustedProxyIdentity(settings.trusted_proxy_identity)
            if settings.trusted_proxy_identity.enabled
            else None
        )
        if settings.token_revocation.enabled and revocations is None:
            raise ConfigurationError(
                "api.token_revocation is enabled but no token revocation store was provided"
            )
        self._revocations = revocations if settings.token_revocation.enabled else None
        self._require_jti = settings.token_revocation.require_jti

    @property
    def oidc(self) -> OidcVerifier | None:
        return self._verifier if isinstance(self._verifier, OidcVerifier) else None

    async def authenticate(
        self,
        *,
        authorization: str,
        proxy_identity: Sequence[str] = (),
        peer: str | None = None,
    ) -> AuthenticatedIdentity:
        try:
            identity = await self._authenticate(authorization, proxy_identity, peer)
        except AuthenticationFailure as failure:
            await self.auditor.failure(failure)
            raise
        await self.auditor.success(identity)
        return identity

    async def _authenticate(
        self, authorization: str, proxy_identity: Sequence[str], peer: str | None
    ) -> AuthenticatedIdentity:
        if self.proxy is not None:
            service = self.proxy.resolve(proxy_identity, peer)
            if service is not None:
                return service
        scheme, _, token = authorization.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise AuthenticationFailure(
                "missing_credentials", method=self._method, detail="bearer token is required"
            )
        identity = await self._verifier.verify(token)
        await self._check_revocation(identity)
        return identity

    async def _check_revocation(self, identity: AuthenticatedIdentity) -> None:
        if self._revocations is None:
            return
        context: dict[str, Any] = {
            "method": identity.method,
            "issuer": identity.issuer,
            "key_id": identity.key_id,
            "subject": identity.user_id,
            "tenant_id": identity.tenant_id,
        }
        jti = identity.claims.get("jti")
        jti = jti if isinstance(jti, str) and jti else None
        if jti is None and self._require_jti:
            raise AuthenticationFailure("missing_jti", **context)
        try:
            state = await self._revocations.state(identity.tenant_id, jti, identity.user_id)
        except Exception as exc:
            raise AuthenticationFailure(
                "revocation_unavailable",
                status_code=503,
                detail="token revocation check is unavailable",
                **context,
            ) from exc
        if state.token_revoked:
            raise AuthenticationFailure("revoked", **context)
        cutoff = state.subject_revoked_before
        if cutoff is not None:
            issued_at = identity.claims.get("iat")
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=UTC)
            # A token without iat cannot prove it postdates the cutoff: refuse it.
            if not isinstance(issued_at, int | float) or (
                datetime.fromtimestamp(issued_at, UTC) <= cutoff
            ):
                raise AuthenticationFailure("revoked", **context)

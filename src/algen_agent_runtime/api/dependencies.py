from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException, Request

from algen_agent_runtime.auth.authenticator import ApiAuthenticator
from algen_agent_runtime.auth.contracts import AuthenticationFailure
from algen_agent_runtime.auth.revocation import TokenRevocationStore
from algen_agent_runtime.config.settings import ApiSettings
from algen_agent_runtime.security.network import OutboundNetwork
from algen_agent_runtime.types.interfaces import AuditLog


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    scopes: frozenset[str]


async def principal(request: Request) -> Principal:
    # This is an intentionally narrow authentication hook. Deployments replace it with JWT/mTLS.
    x_tenant_id = request.headers.get("x-tenant-id", "")
    x_user_id = request.headers.get("x-user-id", "")
    x_scopes = request.headers.get("x-scopes", "")
    if not x_tenant_id.strip() or not x_user_id.strip():
        raise HTTPException(status_code=401, detail="tenant and user identity are required")
    return Principal(x_tenant_id.strip(), x_user_id.strip(), frozenset(x_scopes.split()))


def require_scope(identity: Principal, scope: str) -> None:
    if scope not in identity.scopes and "*" not in identity.scopes:
        raise HTTPException(status_code=403, detail=f"missing scope {scope}")


def require_any_scope(identity: Principal, *scopes: str) -> None:
    if "*" not in identity.scopes and identity.scopes.isdisjoint(scopes):
        raise HTTPException(status_code=403, detail=f"missing one of scopes: {', '.join(scopes)}")


_AUTH_RESULT_STATE = "algen_agent_runtime_auth_result"


def principal_dependency(
    settings: ApiSettings,
    *,
    audits: AuditLog | None = None,
    revocations: TokenRevocationStore | None = None,
    oidc_transport: httpx.AsyncBaseTransport | None = None,
    outbound: OutboundNetwork | None = None,
) -> Any:
    """The FastAPI dependency that authenticates a request into a ``Principal``.

    ``development_headers`` trusts the identity headers (loopback development only). ``jwt``
    and ``oidc`` verify a bearer token through ``ApiAuthenticator``, optionally accept a
    trusted-proxy service identity, check revocation when enabled and record authentication
    audits into ``audits``. The result is cached on the request, so the rate-limit middleware
    and the route authenticate (and audit) a request once.
    """
    if settings.auth_mode == "development_headers":
        return principal

    authenticator = ApiAuthenticator(
        settings,
        audits=audits,
        revocations=revocations,
        oidc_transport=oidc_transport,
        outbound=outbound,
    )
    proxy_header = authenticator.proxy.header if authenticator.proxy is not None else None

    async def verified_principal(request: Request) -> Principal:
        cached = getattr(request.state, _AUTH_RESULT_STATE, None)
        if isinstance(cached, Principal):
            return cached
        if isinstance(cached, HTTPException):
            raise cached
        try:
            identity = await authenticator.authenticate(
                authorization=request.headers.get("authorization", ""),
                proxy_identity=(
                    request.headers.getlist(proxy_header) if proxy_header is not None else ()
                ),
                peer=request.client.host if request.client is not None else None,
            )
        except AuthenticationFailure as failure:
            headers = (
                {"WWW-Authenticate": 'Bearer error="invalid_token"'}
                if failure.status_code == 401
                else None
            )
            error = HTTPException(
                status_code=failure.status_code, detail=failure.detail, headers=headers
            )
            setattr(request.state, _AUTH_RESULT_STATE, error)
            raise error from None
        result = Principal(identity.tenant_id, identity.user_id, identity.scopes)
        setattr(request.state, _AUTH_RESULT_STATE, result)
        return result

    verified_principal.authenticator = authenticator  # type: ignore[attr-defined]
    return verified_principal

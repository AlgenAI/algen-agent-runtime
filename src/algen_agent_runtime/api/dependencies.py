from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

from algen_agent_runtime.config.settings import ApiSettings
from algen_agent_runtime.exceptions.errors import ConfigurationError


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


def principal_dependency(settings: ApiSettings) -> Any:
    if settings.auth_mode == "development_headers":
        return principal

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

    async def jwt_principal(request: Request) -> Principal:
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="bearer token is required")
        options: dict[str, Any] = {
            "algorithms": list(settings.jwt_algorithms),
            "options": {"require": ["exp", "sub", settings.tenant_claim]},
        }
        if settings.jwt_issuer:
            options["issuer"] = settings.jwt_issuer
        if settings.jwt_audience:
            options["audience"] = settings.jwt_audience
        try:
            claims = jwt.decode(token, key, **options)
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail="invalid bearer token") from exc
        tenant_id = str(claims.get(settings.tenant_claim, "")).strip()
        user_id = str(claims.get("sub", "")).strip()
        raw_scopes = claims.get(settings.scopes_claim, "")
        scopes = (
            frozenset(str(item) for item in raw_scopes)
            if isinstance(raw_scopes, list)
            else frozenset(str(raw_scopes).split())
        )
        if not tenant_id or not user_id:
            raise HTTPException(status_code=401, detail="token identity claims are incomplete")
        return Principal(tenant_id, user_id, scopes)

    return jwt_principal

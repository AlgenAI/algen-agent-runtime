"""HTTP routes for connecting OAuth MCP servers (``mcp/oauth.py``).

``POST /v1/mcp/servers/{server}/oauth/start`` returns the provider's authorize URL with a
single-use state bound to the caller's tenant, user and server. The provider sends the browser
to ``GET /v1/mcp/oauth/callback``, the one route without a principal: its state is the
credential, it works once and expires. ``GET .../oauth/status`` and ``GET /v1/mcp/oauth/servers``
report connections; ``DELETE .../oauth`` revokes one. Reads need ``mcp:read`` (or
``mcp:connect``), connecting and revoking need ``mcp:connect``, and a tenant-wide connection
(``token_scope: tenant``) also needs ``mcp:admin``. No response ever carries a token.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse, Response

from algen_agent_runtime.api.dependencies import Principal, require_any_scope, require_scope
from algen_agent_runtime.events.contracts import AuditEvent
from algen_agent_runtime.exceptions.errors import PolicyDeniedError
from algen_agent_runtime.mcp.contracts import MCPOAuthTokenScope
from algen_agent_runtime.mcp.oauth import (
    MCP_OAUTH_CALLBACK_PATH,
    MCPOAuthError,
    MCPOAuthManager,
    MCPOAuthStateError,
)

if TYPE_CHECKING:
    from algen_agent_runtime.orchestration.container import Container

# The callback carries a code and a state in its URL: never cache it, never leak it onward.
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}


def _with_query(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url)
    query = [*parse_qsl(parts.query, keep_blank_values=True), *params.items()]
    return urlunsplit(parts._replace(query=urlencode(query)))


def add_mcp_oauth_routes(app: FastAPI, container: Container, identity: Any) -> None:
    def manager() -> MCPOAuthManager:
        oauth = container.mcp.oauth if container.mcp is not None else None
        if oauth is None:
            raise HTTPException(status_code=404, detail="no OAuth MCP servers are configured")
        return oauth

    def authorize_change(principal: Principal, oauth: MCPOAuthManager, server: str) -> None:
        require_scope(principal, "mcp:connect")
        if oauth.auth(server).token_scope is MCPOAuthTokenScope.TENANT:
            require_scope(principal, "mcp:admin")

    async def audit(
        principal_tenant: str, actor: str, action: str, server: str, **extra: Any
    ) -> None:
        await container.runtime.audits.append(
            AuditEvent(
                action=action,
                outcome="completed",
                tenant_id=principal_tenant,
                actor_id=actor,
                resource_id=server,
                metadata={"mcp_server": server, **extra},
            )
        )

    @app.get("/v1/mcp/oauth/servers")
    async def list_mcp_oauth_connections(principal: Principal = identity) -> dict[str, Any]:
        require_any_scope(principal, "mcp:read", "mcp:connect")
        oauth = manager()
        statuses = [
            (await oauth.status(name, principal.tenant_id, principal.user_id)).model_dump(
                mode="json"
            )
            for name in oauth.server_names
        ]
        return {"servers": statuses}

    @app.post("/v1/mcp/servers/{server}/oauth/start")
    async def start_mcp_oauth(server: str, principal: Principal = identity) -> dict[str, Any]:
        oauth = manager()
        authorize_change(principal, oauth, server)
        try:
            started = await oauth.start(server, principal.tenant_id, principal.user_id)
        except PolicyDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except MCPOAuthError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return started.model_dump(mode="json")

    @app.get(MCP_OAUTH_CALLBACK_PATH)
    async def mcp_oauth_callback(
        state: str | None = Query(default=None, max_length=512),
        code: str | None = Query(default=None, max_length=4096),
        error: str | None = Query(default=None, max_length=256),
    ) -> Response:
        oauth = manager()
        try:
            completion = await oauth.complete(state, code, error)
        except MCPOAuthStateError as exc:
            return JSONResponse(
                status_code=400,
                content={"detail": str(exc), "code": "mcp_oauth_invalid_state"},
                headers=_NO_STORE,
            )
        except MCPOAuthError as exc:
            target = oauth.auth(exc.server).success_redirect_url if exc.server else None
            if target is not None and exc.server is not None:
                return RedirectResponse(
                    _with_query(
                        target,
                        {
                            "mcp_server": exc.server,
                            "status": "error",
                            "error": "authorization_failed",
                        },
                    ),
                    status_code=303,
                    headers=_NO_STORE,
                )
            return JSONResponse(
                status_code=400,
                content={"detail": str(exc), "code": "mcp_oauth_failed"},
                headers=_NO_STORE,
            )
        key = completion.key
        await audit(
            key.tenant_id,
            completion.initiated_by,
            "mcp.oauth.connect",
            key.server,
            token_scope=completion.status.token_scope.value,
        )
        tools_ready = await _ensure_tools(container, key.server, key.tenant_id, key.user_id)
        target = oauth.auth(key.server).success_redirect_url
        if target is not None:
            return RedirectResponse(
                _with_query(target, {"mcp_server": key.server, "status": "connected"}),
                status_code=303,
                headers=_NO_STORE,
            )
        body = {**completion.status.model_dump(mode="json"), "tools_ready": tools_ready}
        return JSONResponse(content=body, headers=_NO_STORE)

    @app.get("/v1/mcp/servers/{server}/oauth/status")
    async def mcp_oauth_status(server: str, principal: Principal = identity) -> dict[str, Any]:
        require_any_scope(principal, "mcp:read", "mcp:connect")
        try:
            status = await manager().status(server, principal.tenant_id, principal.user_id)
        except PolicyDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return status.model_dump(mode="json")

    @app.delete("/v1/mcp/servers/{server}/oauth")
    async def revoke_mcp_oauth(server: str, principal: Principal = identity) -> dict[str, Any]:
        oauth = manager()
        authorize_change(principal, oauth, server)
        try:
            outcome = await oauth.revoke(server, principal.tenant_id, principal.user_id)
        except PolicyDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        await audit(
            principal.tenant_id,
            principal.user_id,
            "mcp.oauth.revoke",
            server,
            removed=outcome.removed,
            provider_revoked=outcome.provider_revoked,
        )
        return {
            "server": server,
            "removed": outcome.removed,
            "provider_revoked": outcome.provider_revoked,
        }


async def _ensure_tools(
    container: Container, server: str, tenant_id: str, user_id: str | None
) -> bool:
    """List an OAuth server's tools through the first connection, if nobody has yet."""
    mcp = container.mcp
    if mcp is None:
        return False
    if server in mcp.discovered_servers:
        return True
    try:
        tools = await mcp.discover_tools(server, tenant_id=tenant_id, user_id=user_id)
        for tool in tools:
            container.tools.register(tool)
    except Exception:
        # The connection is stored either way; the tools come with the next successful listing.
        return False
    container.degraded_dependencies.pop(f"mcp.{server}", None)
    return True

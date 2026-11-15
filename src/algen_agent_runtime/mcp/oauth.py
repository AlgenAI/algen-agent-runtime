"""OAuth for remote MCP servers: the authorization code flow with PKCE, refresh and revocation.

The official ``mcp`` SDK's ``OAuthClientProvider`` runs the whole flow inside one HTTP request
of one process: on a 401 it calls a redirect handler and then awaits a callback handler in the
same coroutine, keeps one token set per provider instance, and builds the code exchange in
private methods. A server-side Runtime needs the flow split across two HTTP requests (start and
callback) that may land on different workers, one token set per tenant or user, and tool calls
that fail fast with "not connected" instead of waiting for a browser. So this module runs the
standard flow itself (RFC 6749 authorization code + RFC 7636 PKCE S256, RFC 8414 / RFC 9728
discovery, RFC 8707 resource indicators, RFC 7009 revocation) with plain ``httpx`` through the
Runtime's SSRF-guarded client, and hands the MCP transport an ``httpx.Auth`` that reads the
stored token. It needs no optional package; connecting to the server still needs ``mcp``.

Secrets handled here (access and refresh tokens, PKCE verifiers, client secrets, the OAuth state)
never enter an exception message, a log line, an event, a span or an audit record.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from algen_agent_runtime.exceptions.errors import (
    AlgenAgentRuntimeError,
    ConfigurationError,
    MCPNotConnectedError,
    NotFoundError,
    PolicyDeniedError,
    ToolExecutionError,
)
from algen_agent_runtime.mcp.contracts import (
    MCPOAuthTokenScope,
    MCPServerConfigBase,
    MCPSseServerConfig,
    MCPStreamableHttpServerConfig,
    OAuthClientAuth,
    oauth_config,
    secure_url,
)
from algen_agent_runtime.mcp.tokens import (
    InMemoryMCPTokenStore,
    MCPOAuthPendingAuthorization,
    MCPOAuthTokenSet,
    MCPTokenKey,
    MCPTokenStore,
    PostgresMCPTokenStore,
)
from algen_agent_runtime.security.encryption import AesGcmCipher
from algen_agent_runtime.security.network import (
    DNSResolver,
    OutboundNetwork,
    create_safe_http_client,
)
from algen_agent_runtime.types.contracts import ErrorKind, utc_now

if TYPE_CHECKING:
    from algen_agent_runtime.config.settings import SecuritySettings, StorageSettings

MCP_OAUTH_START_PATH = "/v1/mcp/servers/{server}/oauth/start"
MCP_OAUTH_CALLBACK_PATH = "/v1/mcp/oauth/callback"
MCP_OAUTH_COMPLETE_PATH = "/v1/mcp/oauth/complete"
MCP_OAUTH_BINDING_COOKIE_PREFIX = "algen_mcp_oauth_"
_MAX_METADATA_BYTES = 262_144
_MAX_TOKEN_CHARS = 16_384
_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class MCPOAuthError(AlgenAgentRuntimeError):
    """The authorization flow failed. Messages are safe to show: no token, code or state."""

    error_kind = ErrorKind.AUTHORIZATION
    # The server the failed authorization was for, once a valid state named it.
    server: str | None = None


class MCPOAuthStateError(MCPOAuthError):
    """The callback's state is missing, unknown, expired or already used (CSRF or replay)."""


class _TokenEndpointError(MCPOAuthError):
    def __init__(self, status_code: int, error: str | None) -> None:
        self.status_code = status_code
        self.error = error
        detail = f" ({error})" if error else ""
        super().__init__(f"token endpoint returned HTTP {status_code}{detail}")


@dataclass(frozen=True)
class AuthorizationServerEndpoints:
    authorization_endpoint: str
    token_endpoint: str
    revocation_endpoint: str | None = None


class MCPOAuthStatus(BaseModel):
    """What the status route shows. Never a token."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    server: str
    token_scope: MCPOAuthTokenScope
    connected: bool
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = ()
    refreshable: bool = False
    connected_at: datetime | None = None
    connected_by: str | None = None


class MCPOAuthStart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    server: str
    token_scope: MCPOAuthTokenScope
    authorize_url: str
    expires_at: datetime
    # The browser-binding nonce and the cookie the callback reads it from. The start route
    # sets the cookie itself; a host on the Runtime's site may set it instead.
    binding_cookie: str = ""
    browser_binding: str = Field(default="", repr=False)


@dataclass(frozen=True)
class MCPOAuthCompletion:
    key: MCPTokenKey
    initiated_by: str
    status: MCPOAuthStatus


@dataclass(frozen=True)
class MCPOAuthRevocation:
    removed: bool
    provider_revoked: bool | None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def state_hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def binding_cookie_name(state: str) -> str:
    """The cookie that carries one authorization's browser-binding nonce."""
    return f"{MCP_OAUTH_BINDING_COOKIE_PREFIX}{state_hash(state)[:16]}"


async def _read_capped(response: httpx.Response, limit: int) -> bytes | None:
    """The body of a streamed response, or None once it exceeds ``limit`` bytes."""
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


def _safe_code(value: Any) -> str | None:
    """A provider error code, kept only when it looks like one (no free text, no secrets)."""
    return value if isinstance(value, str) and _ERROR_CODE.match(value) else None


def _with_query(url: str, params: Mapping[str, str]) -> str:
    parts = urlsplit(url)
    query = [*parse_qsl(parts.query, keep_blank_values=True), *params.items()]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _resolve(reference: str, environment: Mapping[str, str], name: str) -> str:
    variable = reference.removeprefix("env://")
    value = environment.get(variable) or os.getenv(variable)
    if not value:
        raise ConfigurationError(f"{name} environment variable {variable!r} is not set")
    return value


class MCPOAuthManager:
    """Runs the OAuth flow for the configured MCP servers and hands out fresh access tokens."""

    def __init__(
        self,
        servers: Sequence[MCPServerConfigBase],
        store: MCPTokenStore,
        *,
        environment: Mapping[str, str] | None = None,
        security: SecuritySettings | None = None,
        dns_resolver: DNSResolver | None = None,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = utc_now,
        outbound: OutboundNetwork | None = None,
    ) -> None:
        # Only the HTTP transports take `auth`, so every OAuth server has a URL.
        self._servers: dict[str, tuple[str, OAuthClientAuth]] = {}
        for server in servers:
            auth = oauth_config(server)
            if auth is not None and isinstance(
                server, (MCPStreamableHttpServerConfig, MCPSseServerConfig)
            ):
                self._servers[server.name] = (server.url, auth)
        self.store = store
        self._environment = dict(environment or {})
        self._security = security
        self._dns_resolver = dns_resolver
        self._outbound = outbound
        self._http = http_client
        self._owns_http = http_client is None
        self._clock = clock
        self._endpoints: dict[str, AuthorizationServerEndpoints] = {}
        self._discovery_lock = asyncio.Lock()
        self._refresh_locks: dict[MCPTokenKey, asyncio.Lock] = {}
        self._disconnect_listeners: list[Callable[[MCPTokenKey], Awaitable[None]]] = []

    @property
    def server_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._servers))

    def on_disconnect(self, listener: Callable[[MCPTokenKey], Awaitable[None]]) -> None:
        """Called when a stored connection is revoked or found invalid."""
        self._disconnect_listeners.append(listener)

    def auth(self, server: str) -> OAuthClientAuth:
        try:
            return self._servers[server][1]
        except KeyError as exc:
            raise NotFoundError(f"MCP server {server!r} is not configured for OAuth") from exc

    def key_for(self, server: str, tenant_id: str, user_id: str | None) -> MCPTokenKey:
        """The token key for a caller. Per-user servers fail closed without a user_id."""
        auth = self.auth(server)
        if not tenant_id or not tenant_id.strip():
            raise PolicyDeniedError(f"MCP server {server!r} needs a tenant to choose a connection")
        if auth.token_scope is MCPOAuthTokenScope.USER:
            if not user_id or not user_id.strip():
                raise PolicyDeniedError(
                    f"MCP server {server!r} keeps one connection per user and this call has no "
                    "user_id"
                )
            return MCPTokenKey(tenant_id, user_id, server)
        return MCPTokenKey(tenant_id, None, server)

    def server_for_state(self, state: str | None) -> str | None:
        """The configured server a state names (unverified: use it only to pick a page)."""
        if not state or "." not in state:
            return None
        server = state.split(".", 1)[1]
        return server if server in self._servers else None

    def discovery_principal(self, server: str) -> tuple[str, str | None] | None:
        auth = self.auth(server)
        if auth.discovery_tenant_id is None:
            return None
        return auth.discovery_tenant_id, auth.discovery_user_id

    def not_connected(self, server: str) -> MCPNotConnectedError:
        return MCPNotConnectedError(
            server,
            token_scope=self.auth(server).token_scope.value,
            start_path=MCP_OAUTH_START_PATH.format(server=server),
        )

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- the flow ---------------------------------------------------------------------------

    async def start(self, server: str, tenant_id: str, user_id: str) -> MCPOAuthStart:
        """Begin an authorization: a single-use state bound to this tenant, user and server."""
        auth = self.auth(server)
        key = self.key_for(server, tenant_id, user_id)
        endpoints = await self.endpoints(server)
        client_id, _ = self._client_credentials(server, auth)
        # The server name after the random part lets an unbound callback pick the server's
        # page without reading the pending record; the hash still covers the whole state.
        state = f"{secrets.token_urlsafe(32)}.{server}"
        binding = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        expires_at = self._clock() + timedelta(seconds=auth.state_ttl_seconds)
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": auth.redirect_uri,
            "state": state,
            "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
        }
        if auth.scopes:
            params["scope"] = " ".join(auth.scopes)
        if auth.send_resource:
            params["resource"] = self._resource(server)
        params.update(auth.authorize_params)
        await self.store.put_pending(
            MCPOAuthPendingAuthorization(
                state_hash=state_hash(state),
                server=server,
                tenant_id=tenant_id,
                initiated_by=user_id,
                token_user_id=key.user_id,
                code_verifier=SecretStr(verifier),
                redirect_uri=auth.redirect_uri,
                expires_at=expires_at,
                binding_hash=state_hash(binding),
            )
        )
        return MCPOAuthStart(
            server=server,
            token_scope=auth.token_scope,
            authorize_url=_with_query(endpoints.authorization_endpoint, params),
            expires_at=expires_at,
            binding_cookie=binding_cookie_name(state),
            browser_binding=binding,
        )

    async def complete(
        self,
        state: str | None,
        code: str | None,
        error: str | None = None,
        *,
        browser_binding: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> MCPOAuthCompletion:
        """Finish an authorization: check the state and who finishes it, exchange the code.

        The state alone is not enough: whoever holds an authorize URL could otherwise connect
        their own provider account to the starter's principal, or the starter could have a
        victim connect theirs. Pass ``browser_binding`` (the nonce from the cookie the start
        route set in the starter's browser) or the authenticated ``tenant_id`` and ``user_id``
        of the caller finishing it, which must be the ones that started it.
        """
        if not state or len(state) > 512:
            raise MCPOAuthStateError("the callback has no valid state")
        if browser_binding is None and (tenant_id is None or user_id is None):
            raise MCPOAuthStateError(
                "an authorization completes only with its browser binding or the principal "
                "that started it"
            )
        # Taking the pending record deletes it, so a replayed callback finds nothing.
        pending = await self.store.take_pending(state_hash(state))
        if pending is None:
            raise MCPOAuthStateError("unknown, expired or already used authorization state")
        if pending.expires_at <= self._clock():
            raise MCPOAuthStateError("the authorization expired; start it again")
        if browser_binding is not None:
            bound = pending.binding_hash is not None and secrets.compare_digest(
                state_hash(browser_binding), pending.binding_hash
            )
            if not bound:
                raise MCPOAuthStateError(
                    "the callback did not come from the browser that started the authorization"
                )
        elif not (
            secrets.compare_digest(str(tenant_id).encode(), pending.tenant_id.encode())
            and secrets.compare_digest(str(user_id).encode(), pending.initiated_by.encode())
        ):
            raise MCPOAuthStateError(
                "the authorization was started by another user; start it again"
            )
        try:
            auth = self.auth(pending.server)
        except NotFoundError as exc:
            raise MCPOAuthStateError(
                "the authorization's MCP server is no longer configured"
            ) from exc
        if pending.redirect_uri != auth.redirect_uri:
            raise MCPOAuthStateError("the redirect URI changed since the authorization started")
        try:
            return await self._exchange(pending, auth, code, error)
        except MCPOAuthError as exc:
            exc.server = pending.server
            raise

    async def _exchange(
        self,
        pending: MCPOAuthPendingAuthorization,
        auth: OAuthClientAuth,
        code: str | None,
        error: str | None,
    ) -> MCPOAuthCompletion:
        if error is not None:
            reason = _safe_code(error) or "error"
            raise MCPOAuthError(f"the provider did not grant access ({reason})")
        if not code or len(code) > 4096:
            raise MCPOAuthError("the callback has no authorization code")
        key = MCPTokenKey(pending.tenant_id, pending.token_user_id, pending.server)
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending.redirect_uri,
            "code_verifier": pending.code_verifier.get_secret_value(),
        }
        if auth.send_resource:
            data["resource"] = self._resource(pending.server)
        try:
            tokens = await self._token_request(
                pending.server, auth, data, connected_by=pending.initiated_by, previous=None
            )
        except httpx.HTTPError as exc:
            raise MCPOAuthError(
                f"the token endpoint was unreachable ({type(exc).__name__})"
            ) from None
        await self.store.put(key, tokens)
        return MCPOAuthCompletion(
            key=key, initiated_by=pending.initiated_by, status=self._status(key, auth, tokens)
        )

    async def access_token(
        self,
        server: str,
        tenant_id: str,
        user_id: str | None,
        *,
        rejected_token: str | None = None,
    ) -> str:
        """A usable access token, refreshed when near expiry or after the server rejected
        ``rejected_token``. Raises ``MCPNotConnectedError`` when there is none."""
        auth = self.auth(server)
        key = self.key_for(server, tenant_id, user_id)
        tokens = await self.store.get(key)
        if tokens is None:
            raise self.not_connected(server)
        if rejected_token is None and self._fresh(tokens, auth):
            return tokens.access_token.get_secret_value()
        async with self._refresh_locks.setdefault(key, asyncio.Lock()):
            tokens = await self.store.get(key)
            if tokens is None:
                raise self.not_connected(server)
            current = tokens.access_token.get_secret_value()
            if rejected_token is not None and current != rejected_token:
                return current  # another call already refreshed it
            if rejected_token is None and self._fresh(tokens, auth):
                return current
            if tokens.refresh_token is None:
                await self._forget(key)
                raise self.not_connected(server)
            refreshed = await self._refresh(key, auth, tokens)
            return refreshed.access_token.get_secret_value()

    async def status(self, server: str, tenant_id: str, user_id: str | None) -> MCPOAuthStatus:
        auth = self.auth(server)
        key = self.key_for(server, tenant_id, user_id)
        return self._status(key, auth, await self.store.get(key))

    async def revoke(self, server: str, tenant_id: str, user_id: str | None) -> MCPOAuthRevocation:
        """Remove the stored connection, then ask the provider to revoke it if it can."""
        auth = self.auth(server)
        key = self.key_for(server, tenant_id, user_id)
        try:
            tokens = await self.store.get(key)
        except AlgenAgentRuntimeError:
            tokens = None  # an undecryptable row is still removed
        removed = await self._forget(key)
        if tokens is None:
            return MCPOAuthRevocation(removed=removed, provider_revoked=None)
        try:
            endpoints = await self.endpoints(server)
        except (MCPOAuthError, httpx.HTTPError):
            return MCPOAuthRevocation(removed=removed, provider_revoked=None)
        if endpoints.revocation_endpoint is None:
            return MCPOAuthRevocation(removed=removed, provider_revoked=None)
        # Revoking the refresh token revokes the grant (RFC 7009 section 2.1).
        if tokens.refresh_token is not None:
            data = {
                "token": tokens.refresh_token.get_secret_value(),
                "token_type_hint": "refresh_token",
            }
        else:
            data = {
                "token": tokens.access_token.get_secret_value(),
                "token_type_hint": "access_token",
            }
        try:
            status_code, _ = await self._post_form(
                server, auth, endpoints.revocation_endpoint, data
            )
        except httpx.HTTPError:
            return MCPOAuthRevocation(removed=removed, provider_revoked=False)
        return MCPOAuthRevocation(removed=removed, provider_revoked=status_code == 200)

    # -- metadata ---------------------------------------------------------------------------

    async def endpoints(self, server: str) -> AuthorizationServerEndpoints:
        """The authorization server's endpoints: configured, or discovered once and cached."""
        auth = self.auth(server)
        if auth.authorization_endpoint is not None and auth.token_endpoint is not None:
            return AuthorizationServerEndpoints(
                auth.authorization_endpoint, auth.token_endpoint, auth.revocation_endpoint
            )
        cached = self._endpoints.get(server)
        if cached is not None:
            return cached
        async with self._discovery_lock:
            cached = self._endpoints.get(server)
            if cached is None:
                cached = await self._discover(server, auth)
                self._endpoints[server] = cached
            return cached

    async def _discover(self, server: str, auth: OAuthClientAuth) -> AuthorizationServerEndpoints:
        resource = self._resource(server)
        issuer = auth.issuer
        if issuer is None:
            issuer = _origin(resource)
            prm = await self._first_metadata(_protected_resource_urls(resource))
            if prm is not None:
                _check_resource(prm.get("resource"), resource)
                advertised = prm.get("authorization_servers")
                if isinstance(advertised, list) and advertised and isinstance(advertised[0], str):
                    issuer = advertised[0]
        try:
            secure_url(issuer, "authorization server")
        except ValueError as exc:
            raise MCPOAuthError(str(exc)) from exc
        metadata = await self._first_metadata(_authorization_server_urls(issuer))
        if metadata is None:
            raise MCPOAuthError(
                f"no OAuth metadata found for MCP server {server!r}; set "
                "authorization_endpoint and token_endpoint"
            )
        # RFC 8414 section 3.3: the metadata must name the issuer it was fetched for.
        if str(metadata.get("issuer", "")).rstrip("/") != issuer.rstrip("/"):
            raise MCPOAuthError(f"OAuth metadata for {server!r} names a different issuer")
        methods = metadata.get("code_challenge_methods_supported")
        if isinstance(methods, list) and "S256" not in methods:
            raise MCPOAuthError(
                f"the authorization server for {server!r} does not support PKCE S256"
            )
        found: dict[str, str | None] = {}
        for name in ("authorization_endpoint", "token_endpoint", "revocation_endpoint"):
            value = metadata.get(name)
            if value is None:
                found[name] = None
                continue
            try:
                found[name] = secure_url(str(value), name)
            except ValueError as exc:
                raise MCPOAuthError(str(exc)) from exc
        authorization, token = found["authorization_endpoint"], found["token_endpoint"]
        if authorization is None or token is None:
            raise MCPOAuthError(f"OAuth metadata for {server!r} lacks the authorization endpoints")
        return AuthorizationServerEndpoints(
            authorization, token, auth.revocation_endpoint or found["revocation_endpoint"]
        )

    async def _first_metadata(self, urls: Sequence[str]) -> dict[str, Any] | None:
        client = self._client()
        for url in urls:
            try:
                async with client.stream(
                    "GET", url, headers={"Accept": "application/json"}
                ) as response:
                    status_code = response.status_code
                    content = (
                        await _read_capped(response, _MAX_METADATA_BYTES)
                        if status_code == 200
                        else None
                    )
            except httpx.HTTPError as exc:
                raise MCPOAuthError(
                    f"OAuth metadata discovery failed ({type(exc).__name__})"
                ) from None
            if status_code >= 500 or status_code == 429:
                raise MCPOAuthError(f"OAuth metadata discovery failed (HTTP {status_code})")
            if content is None:
                continue
            try:
                body = json.loads(content)
            except ValueError:
                continue
            if isinstance(body, dict):
                return body
        return None

    # -- token endpoint ---------------------------------------------------------------------

    async def _refresh(
        self, key: MCPTokenKey, auth: OAuthClientAuth, tokens: MCPOAuthTokenSet
    ) -> MCPOAuthTokenSet:
        assert tokens.refresh_token is not None
        data = {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token.get_secret_value(),
        }
        if auth.send_resource:
            data["resource"] = self._resource(key.server)
        try:
            refreshed = await self._token_request(
                key.server, auth, data, connected_by=tokens.connected_by, previous=tokens
            )
        except _TokenEndpointError as exc:
            if exc.status_code in {400, 401}:
                # Another worker may have rotated the refresh token first; keep its result.
                current = await self.store.get(key)
                if current is not None and current.refresh_token != tokens.refresh_token:
                    return current
                # invalid_grant and friends: the grant is gone; the user has to connect again.
                await self._forget(key)
                raise self.not_connected(key.server) from None
            raise ToolExecutionError(
                f"could not refresh the OAuth token for MCP server {key.server!r} "
                f"(HTTP {exc.status_code})"
            ) from None
        except MCPOAuthError as exc:
            raise ToolExecutionError(
                f"could not refresh the OAuth token for MCP server {key.server!r}: {exc}"
            ) from None
        except httpx.HTTPError as exc:
            raise ToolExecutionError(
                f"could not refresh the OAuth token for MCP server {key.server!r} "
                f"({type(exc).__name__})"
            ) from None
        await self.store.put(key, refreshed)
        return refreshed

    async def _token_request(
        self,
        server: str,
        auth: OAuthClientAuth,
        data: dict[str, str],
        *,
        connected_by: str,
        previous: MCPOAuthTokenSet | None,
    ) -> MCPOAuthTokenSet:
        endpoints = await self.endpoints(server)
        status_code, content = await self._post_form(server, auth, endpoints.token_endpoint, data)
        try:
            body = json.loads(content) if content is not None else None
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}
        access_token = body.get("access_token")
        if status_code != 200 or body.get("ok") is False:
            raise _TokenEndpointError(status_code, _safe_code(body.get("error")))
        if not isinstance(access_token, str) or not 0 < len(access_token) <= _MAX_TOKEN_CHARS:
            raise _TokenEndpointError(status_code, "invalid_token_response")
        token_type = body.get("token_type", "Bearer")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise _TokenEndpointError(status_code, "unsupported_token_type")
        refresh_token = body.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            refresh_token = (
                previous.refresh_token.get_secret_value()
                if previous and previous.refresh_token
                else None
            )
        expires_in = body.get("expires_in")
        now = self._clock()
        expires_at = (
            now + timedelta(seconds=float(expires_in))
            if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool)
            else None
        )
        scope = body.get("scope")
        scopes = (
            tuple(scope.split())
            if isinstance(scope, str)
            else (previous.scopes if previous else auth.scopes)
        )
        return MCPOAuthTokenSet(
            access_token=SecretStr(access_token),
            refresh_token=SecretStr(refresh_token) if refresh_token else None,
            token_type="Bearer",
            expires_at=expires_at,
            scopes=scopes,
            connected_at=previous.connected_at if previous else now,
            connected_by=connected_by,
        )

    async def _post_form(
        self, server: str, auth: OAuthClientAuth, url: str, data: dict[str, str]
    ) -> tuple[int, bytes | None]:
        """POST a form; the status code and the body (None when larger than the cap)."""
        client_id, client_secret = self._client_credentials(server, auth)
        form = dict(data)
        headers = {"Accept": "application/json"}
        if client_secret is not None and auth.token_endpoint_auth_method == "client_secret_basic":
            pair = f"{quote(client_id, safe='')}:{quote(client_secret, safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        else:
            form["client_id"] = client_id
            if client_secret is not None:
                form["client_secret"] = client_secret
        async with self._client().stream("POST", url, data=form, headers=headers) as response:
            return response.status_code, await _read_capped(response, _MAX_METADATA_BYTES)

    # -- helpers ----------------------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            security = self._security
            self._http = create_safe_http_client(
                allowed_hosts=tuple(security.allowed_http_hosts) if security else (),
                allow_private_networks=security.allow_private_networks if security else False,
                dns_resolver=self._dns_resolver,
                timeout=15.0,
                follow_redirects=False,
                outbound=self._outbound,
            )
        return self._http

    def _client_credentials(self, server: str, auth: OAuthClientAuth) -> tuple[str, str | None]:
        client_id = auth.client_id or _resolve(
            auth.client_id_ref or "", self._environment, f"MCP server {server!r} client_id_ref"
        )
        secret = (
            _resolve(
                auth.client_secret_ref,
                self._environment,
                f"MCP server {server!r} client_secret_ref",
            )
            if auth.client_secret_ref
            else None
        )
        return client_id, secret

    def _resource(self, server: str) -> str:
        return self._servers[server][0]

    def _fresh(self, tokens: MCPOAuthTokenSet, auth: OAuthClientAuth) -> bool:
        if tokens.expires_at is None:
            return True
        margin = timedelta(seconds=auth.refresh_margin_seconds)
        return tokens.expires_at - margin > self._clock()

    def _status(
        self, key: MCPTokenKey, auth: OAuthClientAuth, tokens: MCPOAuthTokenSet | None
    ) -> MCPOAuthStatus:
        if tokens is None:
            return MCPOAuthStatus(server=key.server, token_scope=auth.token_scope, connected=False)
        return MCPOAuthStatus(
            server=key.server,
            token_scope=auth.token_scope,
            connected=True,
            expires_at=tokens.expires_at,
            scopes=tokens.scopes,
            refreshable=tokens.refresh_token is not None,
            connected_at=tokens.connected_at,
            connected_by=tokens.connected_by,
        )

    async def _forget(self, key: MCPTokenKey) -> bool:
        removed = await self.store.delete(key)
        for listener in self._disconnect_listeners:
            await listener(key)
        return removed


def _protected_resource_urls(resource: str) -> list[str]:
    """RFC 9728 section 3.1: path-aware location first, then the origin's."""
    origin, path = _origin(resource), urlsplit(resource).path.rstrip("/")
    urls = [f"{origin}/.well-known/oauth-protected-resource{path}"] if path else []
    return [*urls, f"{origin}/.well-known/oauth-protected-resource"]


def _authorization_server_urls(issuer: str) -> list[str]:
    """RFC 8414 section 3.1 and OpenID Connect Discovery locations for an issuer."""
    origin, path = _origin(issuer), urlsplit(issuer).path.rstrip("/")
    if not path:
        return [
            f"{origin}/.well-known/oauth-authorization-server",
            f"{origin}/.well-known/openid-configuration",
        ]
    return [
        f"{origin}/.well-known/oauth-authorization-server{path}",
        f"{origin}/.well-known/openid-configuration{path}",
        f"{origin}{path}/.well-known/openid-configuration",
    ]


def _check_resource(advertised: Any, resource: str) -> None:
    """RFC 9728 section 3.3: the metadata must be for this server (same origin, path prefix)."""
    if advertised is None:
        return
    if not isinstance(advertised, str):
        raise MCPOAuthError("protected resource metadata has an invalid resource")
    wanted, given = urlsplit(resource), urlsplit(advertised)
    same_origin = (wanted.scheme, wanted.netloc) == (given.scheme, given.netloc)
    prefix = given.path.rstrip("/")
    if not same_origin or not (wanted.path.rstrip("/") + "/").startswith(prefix + "/"):
        raise MCPOAuthError("protected resource metadata is for a different resource")


class StoredTokenAuth(httpx.Auth):
    """Adds the caller's stored access token to every MCP request; one refresh on a 401."""

    def __init__(
        self, manager: MCPOAuthManager, server: str, tenant_id: str, user_id: str | None
    ) -> None:
        self._manager = manager
        self._server = server
        self._tenant_id = tenant_id
        self._user_id = user_id

    def __repr__(self) -> str:
        return f"StoredTokenAuth(server={self._server!r})"

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        raise RuntimeError("StoredTokenAuth works only with httpx.AsyncClient")

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        try:
            token: str | None = await self._manager.access_token(
                self._server, self._tenant_id, self._user_id
            )
        except Exception:
            # The tool checks the connection before each call; here the server's 401 says it.
            token = None
        if token is not None:
            request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code != 401 or token is None:
            return
        try:
            fresh = await self._manager.access_token(
                self._server, self._tenant_id, self._user_id, rejected_token=token
            )
        except Exception:
            return
        if fresh != token:
            request.headers["Authorization"] = f"Bearer {fresh}"
            yield request


def build_mcp_oauth_manager(
    servers: Sequence[MCPServerConfigBase],
    storage: StorageSettings,
    *,
    database: Any,
    environment: Mapping[str, str] | None = None,
    security: SecuritySettings | None = None,
    outbound: OutboundNetwork | None = None,
) -> MCPOAuthManager | None:
    """The manager for the OAuth servers among ``servers``, or None when none uses OAuth."""
    if not any(oauth_config(server) is not None for server in servers):
        return None
    store: MCPTokenStore
    if storage.mcp_oauth_token_store == "postgres":
        assert storage.mcp_oauth_encryption_key is not None
        # Keys resolve and validate before cryptography is imported.
        cipher = AesGcmCipher.from_references(
            storage.mcp_oauth_encryption_key,
            storage.mcp_oauth_previous_encryption_keys,
            environment,
        )
        store = PostgresMCPTokenStore(database, cipher)
    else:
        store = InMemoryMCPTokenStore()
    return MCPOAuthManager(
        servers, store, environment=environment, security=security, outbound=outbound
    )

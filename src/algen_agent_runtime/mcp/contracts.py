from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator

from algen_agent_runtime.tools.contracts import Idempotency, SideEffect
from algen_agent_runtime.types.contracts import StrictModel


class MCPTransportType(StrEnum):
    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamable_http"


class MCPToolPolicyOverride(StrictModel):
    side_effect: SideEffect | None = None
    idempotency: Idempotency | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    max_result_bytes: int | None = Field(default=None, ge=1)


class MCPServerConfigBase(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]+$")
    transport: MCPTransportType
    allowed_tools: frozenset[str] = Field(
        default_factory=lambda: frozenset({"*"}),
        description="Allowed tool names or '*' to allow all discovered tools from this server.",
    )
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_result_bytes: int = Field(default=1_048_576, ge=1)
    prefix: str | None = None
    required: bool = False
    tool_policies: dict[str, MCPToolPolicyOverride] = Field(default_factory=dict)
    trust_tool_annotations: bool = False


class MCPStdioServerConfig(MCPServerConfigBase):
    transport: Literal[MCPTransportType.STDIO] = MCPTransportType.STDIO
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Explicit environment variables passed to the child process. Ambient process secrets are never leaked.",
    )
    cwd: str | None = None


SENSITIVE_HEADER_NAMES = frozenset(
    [
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-token",
        "token",
        "x-auth-token",
        "x-access-token",
    ]
)


def _validate_headers(headers: dict[str, str]) -> dict[str, str]:
    for k in headers:
        low = k.lower()
        if low in SENSITIVE_HEADER_NAMES or "token" in low or "secret" in low or "key" in low:
            raise ValueError(
                f"sensitive header {k!r} must not be passed in plain 'headers'; "
                f"use auth_token_ref or secret_headers with env:// references"
            )
    return headers


def _validate_secret_headers(secret_headers: dict[str, str]) -> dict[str, str]:
    for k, v in secret_headers.items():
        if not v.startswith("env://"):
            raise ValueError(f"secret_headers value for {k!r} must be an env:// reference")
    return secret_headers


class MCPOAuthTokenScope(StrEnum):
    """Whose OAuth connection a tool call uses."""

    TENANT = "tenant"
    USER = "user"


# Parameters the Runtime sets itself on the authorization request; `authorize_params` cannot
# override them.
RESERVED_AUTHORIZE_PARAMS = frozenset(
    {
        "response_type",
        "client_id",
        "redirect_uri",
        "state",
        "code_challenge",
        "code_challenge_method",
        "scope",
        "resource",
    }
)


def secure_url(value: str, field_name: str) -> str:
    """An absolute HTTPS URL without a fragment or credentials; plain HTTP only for localhost."""
    parsed = urlparse(value)
    local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if not parsed.hostname or (parsed.scheme != "https" and not local_http):
        raise ValueError(f"{field_name} must be an absolute https:// URL (http is local-only)")
    if parsed.fragment:
        raise ValueError(f"{field_name} must not contain a fragment")
    if parsed.username or parsed.password:
        raise ValueError(f"{field_name} must not contain credentials")
    return value


class OAuthClientAuth(StrictModel):
    """OAuth 2.1 authorization code flow with PKCE for a remote MCP server.

    The Runtime runs the flow through its ``/v1/mcp`` routes, keeps the tokens in the configured
    MCP token store (encrypted at rest in PostgreSQL), refreshes them, and sends the access token
    on every request to the server. Tokens never reach tool code, the model, logs, events, traces
    or audit records.
    """

    type: Literal["oauth"] = Field(default="oauth", description="The auth kind; only 'oauth'.")
    client_id: str | None = Field(
        default=None,
        description="The OAuth client ID registered with the provider. Set this or client_id_ref.",
    )
    client_id_ref: str | None = Field(
        default=None,
        description="An env:// reference to the client ID, for deployments that keep it outside "
        "YAML. Set this or client_id.",
    )
    client_secret_ref: str | None = Field(
        default=None,
        description="An env:// reference to the client secret. Leave unset for a public client, "
        "which relies on PKCE alone.",
    )
    token_endpoint_auth_method: Literal["client_secret_post", "client_secret_basic"] = Field(
        default="client_secret_post",
        description="How a confidential client sends its secret to the token endpoint. Public "
        "clients send only client_id.",
    )
    scopes: tuple[str, ...] = Field(
        default=(), description="Scopes requested on the authorization request."
    )
    token_scope: MCPOAuthTokenScope = Field(
        default=MCPOAuthTokenScope.TENANT,
        description="'tenant' keeps one connection per tenant; 'user' keeps one per user and "
        "fails closed when a run has no user_id.",
    )
    redirect_uri: str = Field(
        description="The callback URL registered with the provider; it must reach this Runtime's "
        "GET /v1/mcp/oauth/callback route. Only this exact value is ever sent."
    )
    authorization_endpoint: str | None = Field(
        default=None,
        description="Explicit authorization endpoint. With token_endpoint, skips metadata discovery.",
    )
    token_endpoint: str | None = Field(
        default=None, description="Explicit token endpoint. Set with authorization_endpoint."
    )
    revocation_endpoint: str | None = Field(
        default=None,
        description="Explicit RFC 7009 revocation endpoint; otherwise the discovered one, if any.",
    )
    issuer: str | None = Field(
        default=None,
        description="The authorization server to discover metadata from when the MCP server "
        "publishes no protected resource metadata. Defaults to the MCP server's origin.",
    )
    send_resource: bool = Field(
        default=True,
        description="Send the RFC 8707 'resource' parameter (the MCP server URL), as the MCP "
        "authorization spec asks. Turn off for a provider that rejects it.",
    )
    authorize_params: dict[str, str] = Field(
        default_factory=dict,
        description="Extra non-secret query parameters for the authorization request, e.g. "
        "{'prompt': 'consent'}. Parameters the Runtime sets itself are rejected.",
    )
    success_redirect_url: str | None = Field(
        default=None,
        description="Where the callback sends the browser afterwards, with mcp_server and status "
        "(and error) query parameters added. Without it the callback answers with JSON.",
    )
    state_ttl_seconds: int = Field(
        default=600,
        ge=30,
        le=3600,
        description="How long an authorization begun with the start route stays valid.",
    )
    refresh_margin_seconds: int = Field(
        default=60,
        ge=0,
        le=3600,
        description="Refresh an access token this many seconds before it expires.",
    )
    discovery_tenant_id: str | None = Field(
        default=None,
        description="The tenant whose stored connection lists the server's tools at startup. "
        "Without it, tools are listed when the first connection completes.",
    )
    discovery_user_id: str | None = Field(
        default=None,
        description="The user whose stored connection lists tools at startup (token_scope "
        "'user' only, together with discovery_tenant_id).",
    )

    @model_validator(mode="after")
    def validate_oauth(self) -> OAuthClientAuth:
        if (self.client_id is None) == (self.client_id_ref is None):
            raise ValueError("oauth auth needs exactly one of client_id or client_id_ref")
        if self.client_id is not None and not self.client_id.strip():
            raise ValueError("client_id cannot be empty")
        for name in ("client_id_ref", "client_secret_ref"):
            value = getattr(self, name)
            if value is not None and not value.startswith("env://"):
                raise ValueError(f"{name} must be an env:// reference")
        secure_url(self.redirect_uri, "redirect_uri")
        if (self.authorization_endpoint is None) != (self.token_endpoint is None):
            raise ValueError("set authorization_endpoint and token_endpoint together, or neither")
        for name in (
            "authorization_endpoint",
            "token_endpoint",
            "revocation_endpoint",
            "issuer",
            "success_redirect_url",
        ):
            value = getattr(self, name)
            if value is not None:
                secure_url(value, name)
        reserved = sorted(RESERVED_AUTHORIZE_PARAMS.intersection(self.authorize_params))
        if reserved:
            raise ValueError(f"authorize_params cannot set {reserved}")
        for scope in self.scopes:
            if not scope or any(character.isspace() for character in scope):
                raise ValueError("scopes must be non-empty and contain no whitespace")
        if self.discovery_user_id is not None and self.token_scope is not MCPOAuthTokenScope.USER:
            raise ValueError("discovery_user_id applies to token_scope 'user' only")
        if self.token_scope is MCPOAuthTokenScope.USER and (
            (self.discovery_tenant_id is None) != (self.discovery_user_id is None)
        ):
            raise ValueError(
                "token_scope 'user' discovery needs discovery_tenant_id and discovery_user_id"
            )
        return self

    @property
    def public_client(self) -> bool:
        """True when the client has no secret and relies on PKCE alone."""
        return self.client_secret_ref is None


def _validate_auth_choice(
    auth: OAuthClientAuth | None, auth_token_ref: str | None, secret_headers: dict[str, str]
) -> None:
    if auth is None:
        return
    if auth_token_ref is not None:
        raise ValueError("use either auth (OAuth) or auth_token_ref, not both")
    if any(name.lower() == "authorization" for name in secret_headers):
        raise ValueError(
            "auth (OAuth) sets the Authorization header; remove it from secret_headers"
        )


class MCPSseServerConfig(MCPServerConfigBase):
    transport: Literal[MCPTransportType.SSE] = MCPTransportType.SSE
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    secret_headers: dict[str, str] = Field(default_factory=dict)
    auth_token_ref: str | None = Field(
        default=None,
        description="Secret reference for authentication header, e.g. 'env://MCP_AUTH_TOKEN'.",
    )
    auth: OAuthClientAuth | None = Field(
        default=None,
        description="OAuth for servers that accept only OAuth tokens (type: oauth). Leave unset "
        "to keep auth_token_ref or secret_headers.",
    )

    @model_validator(mode="after")
    def validate_secrets_and_headers(self) -> MCPSseServerConfig:
        _validate_headers(self.headers)
        _validate_secret_headers(self.secret_headers)
        if self.auth_token_ref and not self.auth_token_ref.startswith("env://"):
            raise ValueError("auth_token_ref must be an env:// reference")
        _validate_auth_choice(self.auth, self.auth_token_ref, self.secret_headers)
        return self


class MCPStreamableHttpServerConfig(MCPServerConfigBase):
    transport: Literal[MCPTransportType.STREAMABLE_HTTP] = MCPTransportType.STREAMABLE_HTTP
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    secret_headers: dict[str, str] = Field(default_factory=dict)
    auth_token_ref: str | None = Field(
        default=None,
        description="Secret reference for authentication header, e.g. 'env://MCP_AUTH_TOKEN'.",
    )
    auth: OAuthClientAuth | None = Field(
        default=None,
        description="OAuth for servers that accept only OAuth tokens (type: oauth). Leave unset "
        "to keep auth_token_ref or secret_headers.",
    )

    @model_validator(mode="after")
    def validate_secrets_and_headers(self) -> MCPStreamableHttpServerConfig:
        _validate_headers(self.headers)
        _validate_secret_headers(self.secret_headers)
        if self.auth_token_ref and not self.auth_token_ref.startswith("env://"):
            raise ValueError("auth_token_ref must be an env:// reference")
        _validate_auth_choice(self.auth, self.auth_token_ref, self.secret_headers)
        return self


MCPServerConfig = Annotated[
    MCPStdioServerConfig | MCPSseServerConfig | MCPStreamableHttpServerConfig,
    Field(discriminator="transport"),
]


def oauth_config(config: MCPServerConfigBase) -> OAuthClientAuth | None:
    """The server's OAuth settings, or None when it does not use OAuth."""
    auth = getattr(config, "auth", None)
    return auth if isinstance(auth, OAuthClientAuth) else None

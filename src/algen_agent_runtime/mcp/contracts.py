from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

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


class MCPSseServerConfig(MCPServerConfigBase):
    transport: Literal[MCPTransportType.SSE] = MCPTransportType.SSE
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    secret_headers: dict[str, str] = Field(default_factory=dict)
    auth_token_ref: str | None = Field(
        default=None,
        description="Secret reference for authentication header, e.g. 'env://MCP_AUTH_TOKEN'.",
    )

    @model_validator(mode="after")
    def validate_secrets_and_headers(self) -> MCPSseServerConfig:
        _validate_headers(self.headers)
        _validate_secret_headers(self.secret_headers)
        if self.auth_token_ref and not self.auth_token_ref.startswith("env://"):
            raise ValueError("auth_token_ref must be an env:// reference")
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

    @model_validator(mode="after")
    def validate_secrets_and_headers(self) -> MCPStreamableHttpServerConfig:
        _validate_headers(self.headers)
        _validate_secret_headers(self.secret_headers)
        if self.auth_token_ref and not self.auth_token_ref.startswith("env://"):
            raise ValueError("auth_token_ref must be an env:// reference")
        return self


MCPServerConfig = Annotated[
    MCPStdioServerConfig | MCPSseServerConfig | MCPStreamableHttpServerConfig,
    Field(discriminator="transport"),
]

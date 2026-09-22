from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from algen_agent_runtime.types.contracts import StrictModel


class MCPTransportType(StrEnum):
    STDIO = "stdio"
    SSE = "sse"


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


class MCPStdioServerConfig(MCPServerConfigBase):
    transport: Literal[MCPTransportType.STDIO] = MCPTransportType.STDIO
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Explicit environment variables passed to the child process. Ambient process secrets are never leaked.",
    )
    cwd: str | None = None


class MCPSseServerConfig(MCPServerConfigBase):
    transport: Literal[MCPTransportType.SSE] = MCPTransportType.SSE
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    auth_token_ref: str | None = Field(
        default=None,
        description="Secret reference for authentication header, e.g. 'env://MCP_AUTH_TOKEN'.",
    )


MCPServerConfig = Annotated[
    MCPStdioServerConfig | MCPSseServerConfig,
    Field(discriminator="transport"),
]

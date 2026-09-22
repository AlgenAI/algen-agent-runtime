from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from typing import Any

from algen_agent_runtime.exceptions.errors import (
    ConfigurationError,
    ConflictError,
    NotFoundError,
    PolicyDeniedError,
    ToolExecutionError,
)
from algen_agent_runtime.mcp.contracts import (
    MCPServerConfigBase,
    MCPSseServerConfig,
    MCPStdioServerConfig,
    MCPTransportType,
)
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)

# Standard minimal environment keys for process execution
SAFE_ENV_PASSTHROUGH = {"PATH", "SYSTEMROOT", "TEMP", "TMP", "USER", "HOME"}


def _resolve_secret(reference: str, environment: Mapping[str, str] | None = None) -> str:
    if not reference.startswith("env://"):
        raise ConfigurationError(f"secret reference {reference!r} must use 'env://' scheme")
    var_name = reference.removeprefix("env://")
    resolved = (environment or {}).get(var_name) or os.getenv(var_name)
    if not resolved:
        raise ConfigurationError(f"required environment variable {var_name!r} is not set")
    return resolved


class MCPConnection:
    """Manages an active connection and session to an external MCP server."""

    def __init__(
        self,
        config: MCPServerConfigBase,
        exit_stack: AsyncExitStack,
        session: Any,
    ) -> None:
        self.config = config
        self.exit_stack = exit_stack
        self.session = session

    async def close(self) -> None:
        await self.exit_stack.aclose()


class MCPClientManager:
    """Manager for external Model Context Protocol (MCP) servers and tools."""

    def __init__(
        self,
        servers: Sequence[MCPServerConfigBase] = (),
        *,
        environment: Mapping[str, str] | None = None,
        session_factory: Any | None = None,
    ) -> None:
        self._configs: dict[str, MCPServerConfigBase] = {}
        for s in servers:
            if s.name in self._configs:
                raise ConflictError(f"duplicate MCP server name: {s.name!r}")
            self._configs[s.name] = s
        self._environment = environment or {}
        self._connections: dict[str, MCPConnection] = {}
        self._discovered_tools: dict[str, Tool] = {}
        self._session_factory = session_factory

    def add_server(self, config: MCPServerConfigBase) -> None:
        if config.name in self._configs:
            raise ConflictError(f"MCP server {config.name!r} already registered")
        self._configs[config.name] = config

    def get_server(self, name: str) -> MCPServerConfigBase:
        if name not in self._configs:
            raise NotFoundError(f"MCP server {name!r} not configured")
        return self._configs[name]

    async def connect(self, server_name: str) -> MCPConnection:
        """Connect to an MCP server, establishing transport and initialized session."""
        if server_name in self._connections:
            return self._connections[server_name]

        config = self.get_server(server_name)
        stack = AsyncExitStack()

        try:
            if self._session_factory is not None:
                # Pluggable session factory for testing/fakes
                session = await self._session_factory(config, stack)
            elif config.transport == MCPTransportType.STDIO:
                try:
                    from mcp import ClientSession, StdioServerParameters
                    from mcp.client.stdio import stdio_client
                except ImportError as exc:
                    raise PolicyDeniedError(
                        "mcp package is not installed. Install with 'pip install algen-agent-runtime[mcp]'"
                    ) from exc

                assert isinstance(config, MCPStdioServerConfig)
                # Environment filtering: only pass allowlisted SAFE_ENV_PASSTHROUGH + explicit config.env
                sanitized_env = {
                    k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV_PASSTHROUGH
                }
                sanitized_env.update(config.env)

                params = StdioServerParameters(
                    command=config.command,
                    args=config.args,
                    env=sanitized_env,
                    cwd=config.cwd,
                )
                read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
            elif config.transport == MCPTransportType.SSE:
                assert isinstance(config, MCPSseServerConfig)
                headers = dict(config.headers)
                if config.auth_token_ref:
                    token = _resolve_secret(config.auth_token_ref, self._environment)
                    headers["Authorization"] = f"Bearer {token}"

                try:
                    from mcp import ClientSession
                    from mcp.client.sse import sse_client
                except ImportError as exc:
                    raise PolicyDeniedError(
                        "mcp package is not installed. Install with 'pip install algen-agent-runtime[mcp]'"
                    ) from exc

                read_stream, write_stream = await stack.enter_async_context(
                    sse_client(config.url, headers=headers, timeout=config.timeout_seconds)
                )
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
            else:
                raise ConfigurationError(f"unsupported MCP transport: {config.transport}")

            conn = MCPConnection(config=config, exit_stack=stack, session=session)
            self._connections[server_name] = conn
            return conn
        except Exception:
            await stack.aclose()
            raise

    async def disconnect(self, server_name: str) -> None:
        conn = self._connections.pop(server_name, None)
        if conn:
            await conn.close()

    async def close(self) -> None:
        """Close all active MCP server connections."""
        for name in list(self._connections.keys()):
            await self.disconnect(name)

    async def aclose(self) -> None:
        await self.close()

    async def __aenter__(self) -> MCPClientManager:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def discover_tools(self, server_name: str | None = None) -> tuple[Tool, ...]:
        """Discover tools from one or all configured MCP servers."""
        target_names = [server_name] if server_name else list(self._configs.keys())
        discovered: list[Tool] = []

        for name in target_names:
            conn = await self.connect(name)
            config = conn.config
            session = conn.session

            try:
                tools_response = await session.list_tools()
            except Exception as exc:
                raise ToolExecutionError(
                    f"failed to list tools on MCP server {name!r}: {exc}"
                ) from exc

            raw_tools = getattr(tools_response, "tools", tools_response)

            for raw_tool in raw_tools:
                tool_name = getattr(
                    raw_tool, "name", raw_tool.get("name") if isinstance(raw_tool, dict) else None
                )
                if not tool_name:
                    continue

                # Check server allowlist
                if "*" not in config.allowed_tools and tool_name not in config.allowed_tools:
                    continue

                prefix = config.prefix if config.prefix is not None else f"mcp.{config.name}."
                registered_name = f"{prefix}{tool_name}".lower().replace("-", "_")

                if registered_name in self._discovered_tools:
                    raise ConflictError(f"duplicate tool name discovered: {registered_name!r}")

                desc = (
                    getattr(raw_tool, "description", None)
                    or (raw_tool.get("description") if isinstance(raw_tool, dict) else None)
                    or f"MCP tool {tool_name} from {config.name}"
                )

                input_schema = (
                    getattr(raw_tool, "inputSchema", None)
                    or (raw_tool.get("inputSchema") if isinstance(raw_tool, dict) else None)
                    or {"type": "object", "properties": {}}
                )

                if not isinstance(input_schema, dict):
                    input_schema = {"type": "object", "properties": {}}
                if "type" not in input_schema:
                    input_schema["type"] = "object"
                if "properties" not in input_schema:
                    input_schema["properties"] = {}

                tool_def = ToolDefinition(
                    name=registered_name,
                    version="1.0.0",
                    description=desc,
                    input_schema=input_schema,
                    output_schema={"type": "object"},
                    required_permissions=frozenset({f"mcp.{config.name}"}),
                    side_effect=SideEffect.EXTERNAL,
                    idempotency=Idempotency.NON_IDEMPOTENT,
                    timeout_seconds=config.timeout_seconds,
                    max_result_bytes=config.max_result_bytes,
                )

                tool_instance = self._create_tool_instance(tool_def, config, tool_name, session)
                self._discovered_tools[registered_name] = tool_instance
                discovered.append(tool_instance)

        return tuple(discovered)

    def _create_tool_instance(
        self,
        tool_def: ToolDefinition,
        config: MCPServerConfigBase,
        remote_name: str,
        session: Any,
    ) -> Tool:
        async def execute(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
            try:
                async with asyncio.timeout(config.timeout_seconds):
                    result = await session.call_tool(remote_name, arguments)
            except TimeoutError as exc:
                raise ToolExecutionError(
                    f"MCP tool {tool_def.name} timed out after {config.timeout_seconds}s"
                ) from exc
            except Exception as exc:
                raise ToolExecutionError(
                    f"MCP tool {tool_def.name} execution error: {str(exc)[:200]}"
                ) from exc

            is_error = getattr(result, "isError", False)
            content = getattr(result, "content", [])

            # Extract result payload
            extracted_text = []
            structured_data = getattr(result, "structuredContent", None)

            if content:
                for item in content:
                    text = getattr(item, "text", None) or (
                        item.get("text") if isinstance(item, dict) else None
                    )
                    if text:
                        extracted_text.append(text)

            combined_text = "\n".join(extracted_text)

            if is_error:
                error_summary = combined_text[:300] if combined_text else "server reported error"
                raise ToolExecutionError(f"MCP tool {tool_def.name} failed: {error_summary}")

            # Format response
            response_dict: dict[str, Any] = {
                "text": combined_text,
            }
            if structured_data is not None:
                response_dict["data"] = structured_data

            # Size bound check
            serialized = json.dumps(response_dict, ensure_ascii=False)
            if len(serialized.encode("utf-8")) > config.max_result_bytes:
                raise ToolExecutionError(
                    f"MCP tool {tool_def.name} result ({len(serialized)} bytes) exceeded limit of {config.max_result_bytes} bytes"
                )

            return response_dict

        return Tool(tool_def, execute)

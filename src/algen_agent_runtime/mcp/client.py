from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from algen_agent_runtime.config.settings import SecuritySettings
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
    MCPStreamableHttpServerConfig,
    MCPTransportType,
)
from algen_agent_runtime.security.network import DNSResolver, create_safe_http_client
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


def _make_http_client_factory(
    security: SecuritySettings | None = None,
    dns_resolver: DNSResolver | None = None,
) -> Any:
    allowed_hosts = tuple(security.allowed_http_hosts) if security else ()
    allow_private = security.allow_private_networks if security else False

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return create_safe_http_client(
            allowed_hosts=allowed_hosts,
            allow_private_networks=allow_private,
            dns_resolver=dns_resolver,
            headers=headers,
            timeout=timeout if timeout is not None else 30.0,
            auth=auth,
            follow_redirects=False,
        )

    return factory


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
        security: SecuritySettings | None = None,
        dns_resolver: DNSResolver | None = None,
        session_factory: Any | None = None,
        http_client_factory: Any | None = None,
    ) -> None:
        self._configs: dict[str, MCPServerConfigBase] = {}
        for s in servers:
            if s.name in self._configs:
                raise ConflictError(f"duplicate MCP server name: {s.name!r}")
            self._configs[s.name] = s
        self._environment = environment or {}
        self._security = security
        self._dns_resolver = dns_resolver
        self._connections: dict[str, MCPConnection] = {}
        self._discovered_tools: dict[str, Tool] = {}
        self._session_factory = session_factory
        self._http_client_factory = http_client_factory

    @property
    def server_names(self) -> tuple[str, ...]:
        return tuple(self._configs.keys())

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
            elif config.transport == MCPTransportType.STREAMABLE_HTTP:
                try:
                    from mcp import ClientSession
                    from mcp.client.streamable_http import streamablehttp_client
                except ImportError as exc:
                    raise PolicyDeniedError(
                        "mcp package is not installed. Install with 'pip install algen-agent-runtime[mcp]'"
                    ) from exc

                assert isinstance(config, MCPStreamableHttpServerConfig)
                headers = dict(config.headers)
                if config.auth_token_ref:
                    token = _resolve_secret(config.auth_token_ref, self._environment)
                    headers["Authorization"] = f"Bearer {token}"
                for h_name, h_ref in config.secret_headers.items():
                    headers[h_name] = _resolve_secret(h_ref, self._environment)

                http_factory = self._http_client_factory or _make_http_client_factory(
                    self._security, self._dns_resolver
                )
                read_stream, write_stream, _ = await stack.enter_async_context(
                    streamablehttp_client(
                        config.url,
                        headers=headers,
                        timeout=config.timeout_seconds,
                        httpx_client_factory=http_factory,
                    )
                )
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
            elif config.transport == MCPTransportType.SSE:
                assert isinstance(config, MCPSseServerConfig)
                headers = dict(config.headers)
                if config.auth_token_ref:
                    token = _resolve_secret(config.auth_token_ref, self._environment)
                    headers["Authorization"] = f"Bearer {token}"
                for h_name, h_ref in config.secret_headers.items():
                    headers[h_name] = _resolve_secret(h_ref, self._environment)

                try:
                    from mcp import ClientSession
                    from mcp.client.sse import sse_client
                except ImportError as exc:
                    raise PolicyDeniedError(
                        "mcp package is not installed. Install with 'pip install algen-agent-runtime[mcp]'"
                    ) from exc

                http_factory = self._http_client_factory or _make_http_client_factory(
                    self._security, self._dns_resolver
                )
                read_stream, write_stream = await stack.enter_async_context(
                    sse_client(
                        config.url,
                        headers=headers,
                        timeout=config.timeout_seconds,
                        httpx_client_factory=http_factory,
                    )
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
                raw_tools: list[Any] = []
                cursor: str | None = None
                while True:
                    tools_response = await session.list_tools(cursor=cursor)
                    page_tools = getattr(tools_response, "tools", tools_response)
                    if isinstance(page_tools, (list, tuple)):
                        raw_tools.extend(page_tools)
                    else:
                        raw_tools.append(page_tools)
                        break
                    cursor = getattr(tools_response, "nextCursor", None)
                    if not cursor:
                        break
            except Exception as exc:
                raise ToolExecutionError(
                    f"failed to list tools on MCP server {name!r}: {exc}"
                ) from exc

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

                output_schema = (
                    getattr(raw_tool, "outputSchema", None)
                    or (raw_tool.get("outputSchema") if isinstance(raw_tool, dict) else None)
                    or {"type": "object"}
                )
                if not isinstance(output_schema, dict):
                    output_schema = {"type": "object"}

                # Policy classification precedence:
                # 1. Operator tool_policies override
                # 2. Trusted server annotations (if trust_tool_annotations is True)
                # 3. Secure defaults (SideEffect.EXTERNAL, Idempotency.NON_IDEMPOTENT)
                tool_policy = config.tool_policies.get(tool_name)
                classification_source = "secure_default"
                side_effect = SideEffect.EXTERNAL
                idempotency = Idempotency.NON_IDEMPOTENT

                if config.trust_tool_annotations:
                    annotations = getattr(raw_tool, "annotations", None) or (
                        raw_tool.get("annotations") if isinstance(raw_tool, dict) else None
                    )
                    if annotations is not None:
                        destructive = getattr(annotations, "destructiveHint", False) or (
                            annotations.get("destructiveHint", False)
                            if isinstance(annotations, dict)
                            else False
                        )
                        read_only = getattr(annotations, "readOnlyHint", False) or (
                            annotations.get("readOnlyHint", False)
                            if isinstance(annotations, dict)
                            else False
                        )
                        open_world = getattr(annotations, "openWorldHint", False) or (
                            annotations.get("openWorldHint", False)
                            if isinstance(annotations, dict)
                            else False
                        )
                        idempotent = getattr(annotations, "idempotentHint", False) or (
                            annotations.get("idempotentHint", False)
                            if isinstance(annotations, dict)
                            else False
                        )

                        if destructive:
                            side_effect = SideEffect.DESTRUCTIVE
                        elif read_only:
                            side_effect = SideEffect.READ
                        elif open_world:
                            side_effect = SideEffect.EXTERNAL
                        else:
                            side_effect = SideEffect.EXTERNAL

                        idempotency = (
                            Idempotency.IDEMPOTENT if idempotent else Idempotency.NON_IDEMPOTENT
                        )
                        classification_source = "trusted_annotation"

                if tool_policy is not None:
                    classification_source = "operator"
                    if tool_policy.side_effect is not None:
                        side_effect = tool_policy.side_effect
                    if tool_policy.idempotency is not None:
                        idempotency = tool_policy.idempotency

                effective_timeout = (
                    tool_policy.timeout_seconds
                    if tool_policy and tool_policy.timeout_seconds is not None
                    else config.timeout_seconds
                )
                effective_max_bytes = (
                    tool_policy.max_result_bytes
                    if tool_policy and tool_policy.max_result_bytes is not None
                    else config.max_result_bytes
                )

                tool_def = ToolDefinition(
                    name=registered_name,
                    version="1.0.0",
                    description=desc,
                    input_schema=input_schema,
                    output_schema=output_schema,
                    required_permissions=frozenset({f"mcp.{config.name}"}),
                    side_effect=side_effect,
                    idempotency=idempotency,
                    timeout_seconds=effective_timeout,
                    max_result_bytes=effective_max_bytes,
                    audit_metadata={
                        "classification_source": classification_source,
                        "mcp_server": config.name,
                        "mcp_transport": str(config.transport),
                    },
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
                call_task = asyncio.create_task(session.call_tool(remote_name, arguments))
                try:
                    async with asyncio.timeout(tool_def.timeout_seconds):
                        result = await asyncio.shield(call_task)
                except asyncio.CancelledError:
                    call_task.cancel()
                    try:
                        await call_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    raise
            except TimeoutError as exc:
                raise ToolExecutionError(
                    f"MCP tool {tool_def.name} timed out after {tool_def.timeout_seconds}s"
                ) from exc
            except ToolExecutionError:
                raise
            except asyncio.CancelledError:
                raise
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

            # Size bound check - error message must never echo partial content
            serialized = json.dumps(response_dict, ensure_ascii=False)
            if len(serialized.encode("utf-8")) > tool_def.max_result_bytes:
                raise ToolExecutionError(
                    f"MCP tool {tool_def.name} result ({len(serialized)} bytes) exceeded limit of {tool_def.max_result_bytes} bytes"
                )

            return response_dict

        return Tool(tool_def, execute)

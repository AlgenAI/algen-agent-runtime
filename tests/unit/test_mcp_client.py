from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.exceptions.errors import (
    ConfigurationError,
    ConflictError,
    ToolExecutionError,
)
from algen_agent_runtime.mcp.client import SAFE_ENV_PASSTHROUGH, MCPClientManager
from algen_agent_runtime.mcp.contracts import (
    MCPSseServerConfig,
    MCPStdioServerConfig,
)
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.contracts import ToolContext
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry


def _make_context(tool_name: str = "mcp_test") -> ToolContext:
    return ToolContext(
        run_id="run-mcp",
        step_id="step-1",
        tenant_id="tenant-1",
        user_id="user-1",
        permissions=frozenset({"mcp.test_server"}),
        idempotency_key=f"key-{tool_name}",
    )


class FakeMCPSession:
    """In-memory mock of MCP ClientSession for deterministic testing."""

    def __init__(self, tools: list[dict[str, Any]] | None = None) -> None:
        self._tools = tools or [
            {
                "name": "echo",
                "description": "Echo back input text",
                "inputSchema": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
            },
            {
                "name": "add",
                "description": "Add two numbers",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "number"},
                        "b": {"type": "number"},
                    },
                    "required": ["a", "b"],
                },
            },
        ]
        self.call_history: list[tuple[str, dict[str, Any]]] = []
        self.is_closed = False
        self.delay: float = 0.0
        self.return_error = False
        self.oversized_response = False

    async def initialize(self) -> None:
        pass

    async def list_tools(self) -> Any:
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name=t["name"],
                    description=t.get("description"),
                    inputSchema=t.get("inputSchema"),
                )
                for t in self._tools
            ]
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.call_history.append((name, arguments))
        if self.delay > 0:
            await asyncio.sleep(self.delay)

        if self.return_error:
            return SimpleNamespace(
                isError=True,
                content=[
                    SimpleNamespace(text="Internal tool failure details that should be bounded")
                ],
            )

        if self.oversized_response:
            return SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(text="X" * 2_000_000)],
            )

        if name == "echo":
            msg = arguments.get("message", "")
            return SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(text=f"echo: {msg}")],
                structuredContent={"echoed": msg},
            )
        elif name == "add":
            total = arguments.get("a", 0) + arguments.get("b", 0)
            return SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(text=str(total))],
                structuredContent={"sum": total},
            )
        return SimpleNamespace(isError=False, content=[])


@pytest.mark.asyncio
async def test_mcp_discovery_and_schema_conversion() -> None:
    fake_session = FakeMCPSession()

    async def session_factory(config: Any, stack: Any) -> Any:
        return fake_session

    server_config = MCPStdioServerConfig(
        name="test_server",
        command="fake-cmd",
        allowed_tools=frozenset({"*"}),
    )

    manager = MCPClientManager(
        servers=(server_config,),
        session_factory=session_factory,
    )

    tools = await manager.discover_tools()
    assert len(tools) == 2
    tool_names = {t.definition.name for t in tools}
    assert tool_names == {"mcp.test_server.echo", "mcp.test_server.add"}

    echo_tool = next(t for t in tools if t.definition.name == "mcp.test_server.echo")
    assert echo_tool.definition.description == "Echo back input text"
    assert echo_tool.definition.input_schema["required"] == ["message"]
    assert "message" in echo_tool.definition.input_schema["properties"]


@pytest.mark.asyncio
async def test_mcp_tool_allowlist_filtering() -> None:
    fake_session = FakeMCPSession()

    async def session_factory(config: Any, stack: Any) -> Any:
        return fake_session

    # Only allow 'add', exclude 'echo'
    server_config = MCPStdioServerConfig(
        name="math_server",
        command="fake-cmd",
        allowed_tools=frozenset({"add"}),
    )

    manager = MCPClientManager(
        servers=(server_config,),
        session_factory=session_factory,
    )

    tools = await manager.discover_tools()
    assert len(tools) == 1
    assert tools[0].definition.name == "mcp.math_server.add"


@pytest.mark.asyncio
async def test_mcp_tool_execution_via_executor() -> None:
    fake_session = FakeMCPSession()

    async def session_factory(config: Any, stack: Any) -> Any:
        return fake_session

    server_config = MCPStdioServerConfig(
        name="test_server",
        command="fake-cmd",
    )

    manager = MCPClientManager(
        servers=(server_config,),
        session_factory=session_factory,
    )

    tools = await manager.discover_tools()
    registry = ToolRegistry()
    for t in tools:
        registry.register(t)

    executor = ToolExecutor(registry, CompositePolicyEngine())
    ctx = _make_context("echo")

    # Execute echo tool
    result = await executor.execute(
        "mcp.test_server.echo",
        {"message": "Hello from MCP"},
        ctx,
    )
    assert result.value["text"] == "echo: Hello from MCP"
    assert result.value["data"] == {"echoed": "Hello from MCP"}
    assert len(fake_session.call_history) == 1


@pytest.mark.asyncio
async def test_mcp_tool_timeout_enforcement() -> None:
    fake_session = FakeMCPSession()
    fake_session.delay = 0.5  # Exceeds 0.05s timeout

    async def session_factory(config: Any, stack: Any) -> Any:
        return fake_session

    server_config = MCPStdioServerConfig(
        name="test_server",
        command="fake-cmd",
        timeout_seconds=0.05,
    )

    manager = MCPClientManager(
        servers=(server_config,),
        session_factory=session_factory,
    )

    tools = await manager.discover_tools()
    echo_tool = tools[0]
    ctx = _make_context()

    with pytest.raises(ToolExecutionError, match=r"timed out"):
        await echo_tool.execute({"message": "slow"}, ctx)


@pytest.mark.asyncio
async def test_mcp_oversized_result_enforcement() -> None:
    fake_session = FakeMCPSession()
    fake_session.oversized_response = True

    async def session_factory(config: Any, stack: Any) -> Any:
        return fake_session

    server_config = MCPStdioServerConfig(
        name="test_server",
        command="fake-cmd",
        max_result_bytes=1000,
    )

    manager = MCPClientManager(
        servers=(server_config,),
        session_factory=session_factory,
    )

    tools = await manager.discover_tools()
    echo_tool = tools[0]
    ctx = _make_context()

    with pytest.raises(ToolExecutionError, match=r"exceeded limit"):
        await echo_tool.execute({"message": "test"}, ctx)


@pytest.mark.asyncio
async def test_mcp_server_error_sanitization() -> None:
    fake_session = FakeMCPSession()
    fake_session.return_error = True

    async def session_factory(config: Any, stack: Any) -> Any:
        return fake_session

    server_config = MCPStdioServerConfig(
        name="test_server",
        command="fake-cmd",
    )

    manager = MCPClientManager(
        servers=(server_config,),
        session_factory=session_factory,
    )

    tools = await manager.discover_tools()
    echo_tool = tools[0]
    ctx = _make_context()

    with pytest.raises(ToolExecutionError, match=r"Internal tool failure details"):
        await echo_tool.execute({"message": "test"}, ctx)


@pytest.mark.asyncio
async def test_mcp_duplicate_tool_name_rejection() -> None:
    # Two tools with same name on same server
    fake_session = FakeMCPSession(
        tools=[
            {"name": "dup", "inputSchema": {"type": "object"}},
            {"name": "dup", "inputSchema": {"type": "object"}},
        ]
    )

    async def session_factory(config: Any, stack: Any) -> Any:
        return fake_session

    server_config = MCPStdioServerConfig(
        name="dup_server",
        command="fake-cmd",
    )

    manager = MCPClientManager(
        servers=(server_config,),
        session_factory=session_factory,
    )

    with pytest.raises(ConflictError, match=r"duplicate tool name discovered"):
        await manager.discover_tools()


@pytest.mark.asyncio
async def test_mcp_sse_secret_reference_resolution() -> None:
    server_config = MCPSseServerConfig(
        name="remote_mcp",
        url="https://mcp.example.com/sse",
        auth_token_ref="env://MCP_SECRET_KEY",
    )

    # Missing secret should fail fast
    manager = MCPClientManager(
        servers=(server_config,),
        environment={},
    )
    with pytest.raises(ConfigurationError, match=r"MCP_SECRET_KEY.*is not set"):
        await manager.connect("remote_mcp")


def test_mcp_environment_filtering_safe_keys() -> None:
    # Verify SAFE_ENV_PASSTHROUGH does not include common secret keys
    for secret_name in ["AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "DATABASE_URL", "JWT_SECRET"]:
        assert secret_name not in SAFE_ENV_PASSTHROUGH


def test_mcp_container_wiring() -> None:
    settings = AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
            "agents": [
                {
                    "name": "mcp-agent",
                    "version": "1.0.0",
                    "description": "Agent with MCP",
                    "system_instructions": "Helpful",
                    "default_model": {"name": "default", "provider": "mock", "model": "m"},
                }
            ],
        }
    )

    server = MCPStdioServerConfig(
        name="local_calc",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-everything"],
    )

    container = build_container(settings, mcp_servers=(server,))
    assert container.mcp is not None
    assert container.mcp.get_server("local_calc").name == "local_calc"
    # Ensure mcp is included in container resources for lifecycle cleanup
    assert container.mcp in container.resources


@pytest.mark.asyncio
async def test_mcp_real_stdio_fixture_server(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    # Write a minimal FastMCP fixture server script
    server_script = tmp_path / "fixture_server.py"
    server_script.write_text(
        """from mcp.server.fastmcp import FastMCP
mcp = FastMCP("fixture")

@mcp.tool()
def multiply(a: int, b: int) -> int:
    \"\"\"Multiply two numbers.\"\"\"
    return a * b

if __name__ == "__main__":
    mcp.run(transport="stdio")
"""
    )

    server_config = MCPStdioServerConfig(
        name="fixture_srv",
        command=sys.executable,
        args=[str(server_script)],
    )

    manager = MCPClientManager((server_config,))
    try:
        tools = await manager.discover_tools()
        names = {t.definition.name for t in tools}
        assert "mcp.fixture_srv.multiply" in names

        mult_tool = next(t for t in tools if t.definition.name == "mcp.fixture_srv.multiply")
        ctx = _make_context("multiply")
        res = await mult_tool.execute({"a": 7, "b": 6}, ctx)
        assert "42" in res["text"]
    finally:
        await manager.close()

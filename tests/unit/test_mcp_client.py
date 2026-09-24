from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from algen_agent_runtime.config.settings import AppSettings, SecuritySettings
from algen_agent_runtime.exceptions.errors import (
    ConfigurationError,
    ConflictError,
    PolicyDeniedError,
    ToolExecutionError,
)
from algen_agent_runtime.mcp.client import (
    SAFE_ENV_PASSTHROUGH,
    MCPClientManager,
    _make_http_client_factory,
)
from algen_agent_runtime.mcp.contracts import (
    MCPSseServerConfig,
    MCPStdioServerConfig,
    MCPStreamableHttpServerConfig,
    MCPToolPolicyOverride,
)
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    ToolContext,
)
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

    async def list_tools(self, cursor: str | None = None) -> Any:
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name=t["name"],
                    description=t.get("description"),
                    inputSchema=t.get("inputSchema"),
                    outputSchema=t.get("outputSchema"),
                    annotations=t.get("annotations"),
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


@pytest.mark.asyncio
async def test_mcp_missing_dependency_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "mcp", None)
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", None)
    monkeypatch.setitem(sys.modules, "mcp.client.sse", None)

    stdio_mgr = MCPClientManager((MCPStdioServerConfig(name="s1", command="echo"),))
    with pytest.raises(PolicyDeniedError, match=r"pip install algen-agent-runtime\[mcp\]"):
        await stdio_mgr.discover_tools()

    sse_mgr = MCPClientManager((MCPSseServerConfig(name="s2", url="https://example.com/sse"),))
    with pytest.raises(PolicyDeniedError, match=r"pip install algen-agent-runtime\[mcp\]"):
        await sse_mgr.discover_tools()

    streamable_mgr = MCPClientManager(
        (MCPStreamableHttpServerConfig(name="s3", url="https://example.com/mcp"),)
    )
    with pytest.raises(PolicyDeniedError, match=r"pip install algen-agent-runtime\[mcp\]"):
        await streamable_mgr.discover_tools()


@pytest.mark.asyncio
async def test_streamable_http_transport_safe_client_rejection() -> None:
    # 1. Disallowed host rejection
    security_allowlist = SecuritySettings(
        allowed_http_hosts=["api.example.com"],
        allow_private_networks=False,
    )
    factory_allowlist = _make_http_client_factory(security=security_allowlist)
    client1 = factory_allowlist()
    try:
        with pytest.raises(PolicyDeniedError, match=r"not allowlisted"):
            await client1.get("http://untrusted.com/mcp")
    finally:
        await client1.aclose()

    # 2. Private IP rejection
    security_private = SecuritySettings(
        allowed_http_hosts=(),
        allow_private_networks=False,
    )
    factory_private = _make_http_client_factory(security=security_private)
    client2 = factory_private()
    try:
        with pytest.raises(PolicyDeniedError, match=r"non-public or forbidden address"):
            await client2.get("http://192.168.1.1/mcp")
    finally:
        await client2.aclose()


@pytest.mark.asyncio
async def test_mcp_streamable_http_secret_reference_resolution() -> None:
    server_config = MCPStreamableHttpServerConfig(
        name="remote_streamable",
        url="https://mcp.example.com/mcp",
        auth_token_ref="env://MCP_AUTH_KEY",
        secret_headers={"X-Custom-Auth": "env://CUSTOM_KEY"},
    )

    # Missing required env var should fail fast when connecting
    manager_missing = MCPClientManager(
        servers=(server_config,),
        environment={},
    )
    with pytest.raises(ConfigurationError, match=r"MCP_AUTH_KEY.*is not set"):
        await manager_missing.connect("remote_streamable")


@pytest.mark.asyncio
async def test_mcp_required_vs_optional_discovery_in_container() -> None:
    settings = AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
            "agents": [
                {
                    "name": "test-agent",
                    "version": "1.0.0",
                    "description": "test",
                    "system_instructions": "test",
                    "default_model": {"name": "default", "provider": "mock", "model": "m"},
                }
            ],
        }
    )

    # Required server failing discovery aborts container startup with ConfigurationError
    req_server = MCPStdioServerConfig(
        name="failing_req",
        command="nonexistent_binary_that_fails_404",
        required=True,
    )
    req_container = build_container(settings, mcp_servers=(req_server,))
    with pytest.raises(
        ConfigurationError, match=r"failed to discover required MCP server 'failing_req'"
    ):
        await req_container.astart()

    # Optional server failing discovery degrades gracefully
    opt_server = MCPStdioServerConfig(
        name="failing_opt",
        command="nonexistent_binary_that_fails_404",
        required=False,
    )
    opt_container = build_container(settings, mcp_servers=(opt_server,))
    await opt_container.astart()
    try:
        assert "mcp.failing_opt" in opt_container.degraded_dependencies
        # No tools registered from failing_opt
        registered = [t.definition.name for t in opt_container.tools.list()]
        assert not any(t.startswith("mcp.failing_opt.") for t in registered)
    finally:
        await opt_container.aclose()


@pytest.mark.asyncio
async def test_mcp_policy_precedence_and_annotation_mapping() -> None:
    annotated_tools = [
        {
            "name": "get_data",
            "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        },
        {
            "name": "delete_data",
            "inputSchema": {"type": "object"},
            "annotations": {"destructiveHint": True, "idempotentHint": False},
        },
        {
            "name": "custom_override",
            "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": True},
        },
    ]

    fake_session = FakeMCPSession(tools=annotated_tools)

    async def session_factory(config: Any, stack: Any) -> Any:
        return fake_session

    # Case 1: trust_tool_annotations=False (default) -> secure defaults
    cfg_untrusted = MCPStdioServerConfig(
        name="untrusted_srv",
        command="cmd",
        trust_tool_annotations=False,
    )
    mgr1 = MCPClientManager(servers=(cfg_untrusted,), session_factory=session_factory)
    tools1 = await mgr1.discover_tools()
    for t in tools1:
        assert t.definition.side_effect == SideEffect.EXTERNAL
        assert t.definition.idempotency == Idempotency.NON_IDEMPOTENT
        assert t.definition.audit_metadata["classification_source"] == "secure_default"

    # Case 2: trust_tool_annotations=True -> trusted annotations applied
    cfg_trusted = MCPStdioServerConfig(
        name="trusted_srv",
        command="cmd",
        trust_tool_annotations=True,
    )
    mgr2 = MCPClientManager(servers=(cfg_trusted,), session_factory=session_factory)
    tools2 = {t.definition.name: t for t in await mgr2.discover_tools()}

    get_tool = tools2["mcp.trusted_srv.get_data"]
    assert get_tool.definition.side_effect == SideEffect.READ
    assert get_tool.definition.idempotency == Idempotency.IDEMPOTENT
    assert get_tool.definition.audit_metadata["classification_source"] == "trusted_annotation"

    del_tool = tools2["mcp.trusted_srv.delete_data"]
    assert del_tool.definition.side_effect == SideEffect.DESTRUCTIVE
    assert del_tool.definition.idempotency == Idempotency.NON_IDEMPOTENT
    assert del_tool.definition.audit_metadata["classification_source"] == "trusted_annotation"

    # Case 3: Operator tool_policies override takes precedence over annotations
    cfg_operator = MCPStdioServerConfig(
        name="override_srv",
        command="cmd",
        trust_tool_annotations=True,
        tool_policies={
            "custom_override": MCPToolPolicyOverride(
                side_effect=SideEffect.DESTRUCTIVE,
                idempotency=Idempotency.NON_IDEMPOTENT,
            )
        },
    )
    mgr3 = MCPClientManager(servers=(cfg_operator,), session_factory=session_factory)
    tools3 = {t.definition.name: t for t in await mgr3.discover_tools()}
    override_tool = tools3["mcp.override_srv.custom_override"]
    assert override_tool.definition.side_effect == SideEffect.DESTRUCTIVE
    assert override_tool.definition.audit_metadata["classification_source"] == "operator"


@pytest.mark.asyncio
async def test_mcp_tool_pagination() -> None:
    class PaginatedFakeMCPSession:
        async def initialize(self) -> None:
            pass

        async def list_tools(self, cursor: str | None = None) -> Any:
            if cursor is None:
                return SimpleNamespace(
                    tools=[SimpleNamespace(name="tool_page1", description="p1", inputSchema={})],
                    nextCursor="cursor-page2",
                )
            elif cursor == "cursor-page2":
                return SimpleNamespace(
                    tools=[SimpleNamespace(name="tool_page2", description="p2", inputSchema={})],
                    nextCursor=None,
                )
            return SimpleNamespace(tools=[], nextCursor=None)

    fake_session = PaginatedFakeMCPSession()

    async def session_factory(config: Any, stack: Any) -> Any:
        return fake_session

    cfg = MCPStdioServerConfig(name="paginated_srv", command="cmd")
    mgr = MCPClientManager(servers=(cfg,), session_factory=session_factory)
    tools = await mgr.discover_tools()
    tool_names = {t.definition.name for t in tools}
    assert tool_names == {"mcp.paginated_srv.tool_page1", "mcp.paginated_srv.tool_page2"}


@pytest.mark.asyncio
async def test_mcp_cancellation_safety() -> None:
    fake_session = FakeMCPSession()
    fake_session.delay = 2.0  # Slow call

    async def session_factory(config: Any, stack: Any) -> Any:
        return fake_session

    cfg = MCPStdioServerConfig(name="cancel_srv", command="cmd", timeout_seconds=10.0)
    mgr = MCPClientManager(servers=(cfg,), session_factory=session_factory)
    tools = await mgr.discover_tools()
    echo_tool = tools[0]
    ctx = _make_context()

    task = asyncio.create_task(echo_tool.execute({"message": "test"}, ctx))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_mcp_container_shutdown_closes_clients() -> None:
    settings = AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
            "agents": [
                {
                    "name": "test-agent",
                    "version": "1.0.0",
                    "description": "test",
                    "system_instructions": "test",
                    "default_model": {"name": "default", "provider": "mock", "model": "m"},
                }
            ],
        }
    )
    fake_session = FakeMCPSession()

    async def session_factory(config: Any, stack: Any) -> Any:
        return fake_session

    server = MCPStdioServerConfig(name="srv_close", command="cmd")
    container = build_container(settings, mcp_servers=(server,))
    assert container.mcp is not None
    container.mcp._session_factory = session_factory

    await container.astart()
    tool_names = [t.definition.name for t in container.tools.list()]
    assert "mcp.srv_close.echo" in tool_names
    assert len(container.mcp._connections) == 1

    await container.aclose()
    assert len(container.mcp._connections) == 0


@pytest.mark.asyncio
async def test_mcp_real_inproc_fastmcp_suite() -> None:
    pytest.importorskip("mcp")
    import anyio
    from mcp.client.session import ClientSession
    from mcp.server.fastmcp import FastMCP

    fastmcp_server = FastMCP("inproc_suite")

    @fastmcp_server.tool()
    def calculate_tax(amount: float, rate: float = 0.1) -> float:
        """Calculate sales tax."""
        return amount * rate

    @fastmcp_server.tool()
    def failing_tool() -> str:
        """Always fails."""
        raise RuntimeError("Something went wrong inside FastMCP tool")

    async def session_factory(config: Any, stack: AsyncExitStack) -> Any:
        server_send, client_recv = anyio.create_memory_object_stream(100)
        client_send, server_recv = anyio.create_memory_object_stream(100)

        task = asyncio.create_task(
            fastmcp_server._mcp_server.run(
                server_recv,
                server_send,
                fastmcp_server._mcp_server.create_initialization_options(),
            )
        )

        async def cleanup() -> None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        stack.push_async_callback(cleanup)

        session = await stack.enter_async_context(ClientSession(client_recv, client_send))
        await session.initialize()
        return session

    server_config = MCPStdioServerConfig(
        name="fastmcp_suite",
        command="inproc",
        trust_tool_annotations=True,
    )

    manager = MCPClientManager(
        servers=(server_config,),
        session_factory=session_factory,
    )

    try:
        # 1. Discovery & schema check
        tools = await manager.discover_tools()
        tool_names = {t.definition.name for t in tools}
        assert "mcp.fastmcp_suite.calculate_tax" in tool_names
        assert "mcp.fastmcp_suite.failing_tool" in tool_names

        tax_tool = next(t for t in tools if t.definition.name == "mcp.fastmcp_suite.calculate_tax")
        assert "amount" in tax_tool.definition.input_schema["properties"]

        # 2. Structured execution
        ctx = _make_context("calculate_tax")
        tax_res = await tax_tool.execute({"amount": 100.0, "rate": 0.2}, ctx)
        assert tax_res["text"] == "20.0"
        assert tax_res["data"] == {"result": 20.0}

        # 3. Tool error handling
        fail_tool = next(t for t in tools if t.definition.name == "mcp.fastmcp_suite.failing_tool")
        with pytest.raises(ToolExecutionError, match=r"Something went wrong"):
            await fail_tool.execute({}, ctx)
    finally:
        await manager.close()

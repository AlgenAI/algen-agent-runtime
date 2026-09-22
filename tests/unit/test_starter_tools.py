from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.exceptions.errors import NotFoundError, PolicyDeniedError
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.tools.contracts import ToolContext
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.tools.starter import (
    calculator_tool,
    clock_tool,
    directory_list_tool,
    file_read_tool,
    get_starter_tools,
    json_query_tool,
    register_starter_tools,
)


def _make_context(tool_name: str = "test") -> ToolContext:
    return ToolContext(
        run_id="run-1",
        step_id="step-1",
        tenant_id="tenant-1",
        user_id="user-1",
        permissions=frozenset({"fs.read"}),
        idempotency_key=f"key-{tool_name}",
    )


# ---------------------------------------------------------------------------
# Calculator Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calculator_basic_arithmetic() -> None:
    tool = calculator_tool()
    ctx = _make_context("calc")

    res = await tool.execute({"expression": "2 + 2 * 3"}, ctx)
    assert res == {"result": 8}

    res = await tool.execute({"expression": "(10 - 4) / 2"}, ctx)
    assert res == {"result": 3.0}

    res = await tool.execute({"expression": "7 // 2 + 7 % 2"}, ctx)
    assert res == {"result": 4}

    res = await tool.execute({"expression": "2 ** 10"}, ctx)
    assert res == {"result": 1024}


@pytest.mark.asyncio
async def test_calculator_safe_functions() -> None:
    tool = calculator_tool()
    ctx = _make_context("calc")

    res = await tool.execute({"expression": "abs(-42)"}, ctx)
    assert res == {"result": 42}

    res = await tool.execute({"expression": "round(3.14159, 2)"}, ctx)
    assert res == {"result": 3.14}

    res = await tool.execute({"expression": "max(1, 10, 5) + min(20, 3)"}, ctx)
    assert res == {"result": 13}

    res = await tool.execute({"expression": "sqrt(144)"}, ctx)
    assert res == {"result": 12.0}


@pytest.mark.asyncio
async def test_calculator_division_by_zero() -> None:
    tool = calculator_tool()
    ctx = _make_context("calc")

    with pytest.raises(ValueError, match="division by zero"):
        await tool.execute({"expression": "10 / 0"}, ctx)

    with pytest.raises(ValueError, match="modulo by zero"):
        await tool.execute({"expression": "10 % 0"}, ctx)


@pytest.mark.asyncio
async def test_calculator_rejects_disallowed_syntax_and_calls() -> None:
    tool = calculator_tool()
    ctx = _make_context("calc")

    with pytest.raises(ValueError, match=r"disallowed syntax|disallowed syntax: Name"):
        await tool.execute({"expression": "x + 1"}, ctx)

    with pytest.raises(ValueError, match="unsupported or disallowed function call"):
        await tool.execute({"expression": "open('/etc/passwd')"}, ctx)

    with pytest.raises(ValueError, match=r"disallowed syntax|disallowed function call"):
        await tool.execute({"expression": "__import__('os').system('ls')"}, ctx)

    with pytest.raises(ValueError, match="disallowed syntax"):
        await tool.execute({"expression": "[x for x in (1, 2)]"}, ctx)


@pytest.mark.asyncio
async def test_calculator_arithmetic_abuse_and_bounds() -> None:
    tool = calculator_tool()
    ctx = _make_context("calc")

    # Exponent > 1000
    with pytest.raises(ValueError, match=r"exponent .* exceeds limit"):
        await tool.execute({"expression": "2 ** 1001"}, ctx)

    # Nesting depth > 15
    nested = "(" * 17 + "1" + " + 1)" * 17
    with pytest.raises(ValueError, match="maximum nesting depth"):
        await tool.execute({"expression": nested}, ctx)

    # Empty expression
    with pytest.raises(ValueError, match="empty expression"):
        await tool.execute({"expression": "   "}, ctx)


# ---------------------------------------------------------------------------
# JSON Query Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_query_navigation() -> None:
    tool = json_query_tool()
    ctx = _make_context("json")
    data = {
        "store": {
            "book": [
                {"title": "Book 1", "price": 10},
                {"title": "Book 2", "price": 20},
            ],
            "owner": "Alice",
        }
    }

    res = await tool.execute({"data": data, "path": "store.owner"}, ctx)
    assert res == {"result": "Alice"}

    res = await tool.execute({"data": data, "path": "store.book[1].price"}, ctx)
    assert res == {"result": 20}

    # Wildcard projection
    res = await tool.execute({"data": data, "path": "store.book[*].title"}, ctx)
    assert res == {"result": ["Book 1", "Book 2"]}


@pytest.mark.asyncio
async def test_json_query_operations_and_string_input() -> None:
    tool = json_query_tool()
    ctx = _make_context("json")
    raw_json = '{"users": ["Alice", "Bob"], "meta": {"count": 2, "active": true}}'

    # get string input
    res = await tool.execute({"data": raw_json, "path": "users[0]"}, ctx)
    assert res == {"result": "Alice"}

    # keys
    res = await tool.execute({"data": raw_json, "path": "meta", "operation": "keys"}, ctx)
    assert set(res["result"]) == {"count", "active"}

    # length
    res = await tool.execute({"data": raw_json, "path": "users", "operation": "length"}, ctx)
    assert res == {"result": 2}


@pytest.mark.asyncio
async def test_json_query_errors() -> None:
    tool = json_query_tool()
    ctx = _make_context("json")

    # Malformed JSON
    with pytest.raises(ValueError, match="malformed JSON input"):
        await tool.execute({"data": "{bad json"}, ctx)

    # Missing key
    with pytest.raises(KeyError, match="not found"):
        await tool.execute({"data": {"a": 1}, "path": "b"}, ctx)

    # Index out of range
    with pytest.raises(IndexError, match="out of range"):
        await tool.execute({"data": [1, 2], "path": "[5]"}, ctx)


# ---------------------------------------------------------------------------
# Filesystem Read and Directory List Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fs_file_read_and_dir_list(tmp_path: Path) -> None:
    # Setup workspace
    sub = tmp_path / "sub"
    sub.mkdir()
    f1 = tmp_path / "hello.txt"
    f1.write_text("Hello, Algen!", encoding="utf-8")
    f2 = sub / "nested.txt"
    f2.write_text("Nested content", encoding="utf-8")

    read_tool = file_read_tool(workspace_root=tmp_path)
    list_tool = directory_list_tool(workspace_root=tmp_path)
    ctx = _make_context("fs")

    # Read file
    res = await read_tool.execute({"path": "hello.txt"}, ctx)
    assert res["content"] == "Hello, Algen!"
    assert res["total_bytes"] == len("Hello, Algen!")
    assert not res["truncated"]

    # Read nested file with offset and max_bytes
    res = await read_tool.execute({"path": "sub/nested.txt", "offset": 7, "max_bytes": 7}, ctx)
    assert res["content"] == "content"

    # List directory
    list_res = await list_tool.execute({"path": "", "recursive": True}, ctx)
    paths = {e["path"] for e in list_res["entries"]}
    assert "hello.txt" in paths
    assert "sub" in paths
    assert "sub/nested.txt" in paths


@pytest.mark.asyncio
async def test_fs_path_traversal_protection(tmp_path: Path) -> None:
    read_tool = file_read_tool(workspace_root=tmp_path)
    list_tool = directory_list_tool(workspace_root=tmp_path)
    ctx = _make_context("fs")

    with pytest.raises(PolicyDeniedError, match="resolves outside workspace"):
        await read_tool.execute({"path": "../../etc/passwd"}, ctx)

    with pytest.raises(PolicyDeniedError, match="resolves outside workspace"):
        await list_tool.execute({"path": "../.."}, ctx)


@pytest.mark.asyncio
async def test_fs_symlink_escape_protection(tmp_path: Path) -> None:
    outside_file = tmp_path.parent / "secret_outside.txt"
    outside_file.write_text("top-secret", encoding="utf-8")

    symlink_file = tmp_path / "escape_link.txt"
    try:
        os.symlink(outside_file, symlink_file)
    except OSError:
        pytest.skip("Symlinks not supported on this platform")

    read_tool = file_read_tool(workspace_root=tmp_path)
    ctx = _make_context("fs")

    with pytest.raises(
        PolicyDeniedError,
        match=r"resolves outside workspace root|symlink escaping workspace root",
    ):
        await read_tool.execute({"path": "escape_link.txt"}, ctx)


@pytest.mark.asyncio
async def test_fs_binary_file_rejection(tmp_path: Path) -> None:
    bin_file = tmp_path / "binary.bin"
    bin_file.write_bytes(b"\x00\x01\x02\x03\x04\x00")

    read_tool = file_read_tool(workspace_root=tmp_path)
    ctx = _make_context("fs")

    with pytest.raises(ValueError, match="binary data"):
        await read_tool.execute({"path": "binary.bin"}, ctx)


@pytest.mark.asyncio
async def test_fs_file_not_found(tmp_path: Path) -> None:
    read_tool = file_read_tool(workspace_root=tmp_path)
    ctx = _make_context("fs")

    with pytest.raises(NotFoundError, match="file not found"):
        await read_tool.execute({"path": "does_not_exist.txt"}, ctx)


# ---------------------------------------------------------------------------
# Clock Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clock_tool_deterministic() -> None:
    fixed_dt = datetime(2026, 9, 22, 10, 30, 0, tzinfo=UTC)
    tool = clock_tool(now_fn=lambda: fixed_dt)
    ctx = _make_context("clock")

    # UTC
    res = await tool.execute({"timezone": "UTC"}, ctx)
    assert res["year"] == 2026
    assert res["month"] == 9
    assert res["day"] == 22
    assert res["hour"] == 10
    assert res["minute"] == 30
    assert res["timezone"] == "UTC"

    # Specific timezone
    res_ny = await tool.execute({"timezone": "America/New_York"}, ctx)
    assert res_ny["year"] == 2026
    assert res_ny["hour"] == 6  # EDT is UTC-4
    assert res_ny["timezone"] == "America/New_York"


@pytest.mark.asyncio
async def test_clock_invalid_timezone() -> None:
    tool = clock_tool()
    ctx = _make_context("clock")

    with pytest.raises(ValueError, match="unknown timezone"):
        await tool.execute({"timezone": "Invalid/Fake_Zone"}, ctx)


# ---------------------------------------------------------------------------
# Opt-In / Registration Tests
# ---------------------------------------------------------------------------


def test_starter_tools_disabled_by_default_in_container() -> None:
    settings = AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
            "agents": [
                {
                    "name": "test-agent",
                    "version": "1.0.0",
                    "description": "Test",
                    "system_instructions": "Hello",
                    "default_model": {"name": "default", "provider": "mock", "model": "m"},
                }
            ],
        }
    )
    container = build_container(settings)

    # By default, only core.http and core.subprocess are registered
    registered_names = {t.definition.name for t in container.tools.list()}
    assert registered_names == {"core.http", "core.subprocess"}
    assert "starter.calculator" not in registered_names


def test_starter_tools_explicit_registration_via_additional_tools() -> None:
    settings = AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
            "agents": [
                {
                    "name": "test-agent",
                    "version": "1.0.0",
                    "description": "Test",
                    "system_instructions": "Hello",
                    "default_model": {"name": "default", "provider": "mock", "model": "m"},
                }
            ],
        }
    )
    starter = get_starter_tools()
    container = build_container(settings, additional_tools=starter)

    registered_names = {t.definition.name for t in container.tools.list()}
    assert "starter.calculator" in registered_names
    assert "starter.json_query" in registered_names
    assert "starter.file_read" in registered_names
    assert "starter.directory_list" in registered_names
    assert "starter.clock" in registered_names


def test_starter_tools_explicit_registration_via_helper() -> None:
    registry = ToolRegistry()
    register_starter_tools(registry)
    registered_names = {t.definition.name for t in registry.list()}
    assert registered_names == {
        "starter.calculator",
        "starter.clock",
        "starter.directory_list",
        "starter.file_read",
        "starter.json_query",
    }


@pytest.mark.asyncio
async def test_starter_tools_executor_validation_and_execution() -> None:
    from algen_agent_runtime.exceptions.errors import ToolExecutionError
    from algen_agent_runtime.policies.engine import CompositePolicyEngine
    from algen_agent_runtime.tools.executor import ToolExecutor

    registry = ToolRegistry()
    register_starter_tools(registry)
    executor = ToolExecutor(registry, CompositePolicyEngine())
    ctx = _make_context("executor_test")

    # Valid calculator call
    calc_res = await executor.execute("starter.calculator", {"expression": "10 * 5"}, ctx)
    assert calc_res.value == {"result": 50}

    # Invalid calculator input schema (missing expression)
    with pytest.raises(ToolExecutionError, match=r"invalid input for starter\.calculator"):
        await executor.execute("starter.calculator", {}, ctx)

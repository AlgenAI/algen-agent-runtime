"""Sandbox policy for subprocesses: environment, limits, wrapper, process-group kill."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from algen_agent_runtime.config.settings import AppSettings, SecuritySettings
from algen_agent_runtime.exceptions.errors import PolicyDeniedError, ToolExecutionError
from algen_agent_runtime.mcp.client import MCPClientManager
from algen_agent_runtime.mcp.contracts import MCPStdioServerConfig
from algen_agent_runtime.security.sandbox import (
    SandboxSettings,
    SubprocessSandbox,
    prepare_command,
    run_sandboxed,
)
from algen_agent_runtime.tools.builtin import subprocess_tool
from algen_agent_runtime.tools.contracts import ToolContext

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process sandboxing")

REPORT = """
import json, os, resource
limits = {name: resource.getrlimit(getattr(resource, name))[0]
          for name in ("RLIMIT_CPU", "RLIMIT_NOFILE", "RLIMIT_FSIZE")}
print(json.dumps({"env": dict(os.environ), "limits": limits, "cwd": os.path.realpath(os.getcwd())}))
"""


def _context() -> ToolContext:
    return ToolContext(
        run_id="run",
        step_id="step",
        tenant_id="tenant",
        user_id="user",
        idempotency_key="key",
        permissions=frozenset({"process.execute"}),
    )


async def _report(policy: SubprocessSandbox, **kwargs: Any) -> dict[str, Any]:
    prepared = prepare_command(
        sys.executable, ["-c", REPORT], policy=policy, required=False, label="test", **kwargs
    )
    code, stdout, stderr = await run_sandboxed(prepared, timeout_seconds=30)
    assert code == 0, stderr.decode()
    result: dict[str, Any] = json.loads(stdout)
    return result


async def test_environment_is_scrubbed_to_the_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANDBOX_TEST_SECRET", "do-not-leak")
    monkeypatch.setenv("SANDBOX_TEST_KEEP", "kept")
    report = await _report(
        SubprocessSandbox(env_allowlist=("SANDBOX_TEST_KEEP",), working_directory=str(tmp_path)),
        explicit_env={"EXPLICIT": "value"},
    )
    # The platform may add these in the child: Python's locale coercion (PEP 538) and
    # macOS's CoreFoundation text encoding.
    for added in ("LC_CTYPE", "__CF_USER_TEXT_ENCODING"):
        report["env"].pop(added, None)
    assert report["env"] == {"SANDBOX_TEST_KEEP": "kept", "EXPLICIT": "value"}
    assert report["cwd"] == str(tmp_path)  # pytest's tmp_path is already resolved


async def test_resource_limits_apply_to_the_child() -> None:
    report = await _report(
        SubprocessSandbox(
            env_allowlist=(),
            limits={"cpu_seconds": 20, "open_files": 64, "file_size_bytes": 1_048_576},
        )
    )
    assert report["limits"] == {
        "RLIMIT_CPU": 20,
        "RLIMIT_NOFILE": 64,
        "RLIMIT_FSIZE": 1_048_576,
    }


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is enforced on Linux")
async def test_memory_limit_applies_on_linux() -> None:
    prepared = prepare_command(
        sys.executable,
        ["-c", "import resource; print(resource.getrlimit(resource.RLIMIT_AS)[0])"],
        policy=SubprocessSandbox(limits={"memory_bytes": 512 * 1024 * 1024}),
        required=False,
        label="test",
    )
    code, stdout, _ = await run_sandboxed(prepared, timeout_seconds=30)
    assert code == 0 and int(stdout) == 512 * 1024 * 1024


async def test_the_default_policy_changes_nothing() -> None:
    prepared = prepare_command(
        "server", ["--flag"], policy=SubprocessSandbox(), required=False, label="test"
    )
    assert (prepared.command, prepared.args) == ("server", ["--flag"])


async def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "background.pid"
    script = f"sleep 60 & echo $! > {pid_file}; wait"
    prepared = prepare_command(
        "/bin/sh", ["-c", script], policy=SubprocessSandbox(), required=False, label="test"
    )
    with pytest.raises(ToolExecutionError, match="timed out"):
        await run_sandboxed(prepared, timeout_seconds=1)
    background = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(background, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)
    pytest.fail("the background process outlived the timeout")


def _wrapper(tmp_path: Path) -> Path:
    marker = tmp_path / "wrapped"
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(f'#!/bin/sh\necho yes > {marker}\nexec "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return wrapper


async def test_subprocess_tool_requires_a_wrapper_when_configured(tmp_path: Path) -> None:
    tool = subprocess_tool(enabled=True, sandbox=SandboxSettings(required=True))
    with pytest.raises(PolicyDeniedError, match="no sandbox wrapper"):
        await tool.execute({"executable": "/bin/echo", "arguments": ["hi"]}, _context())

    wrapper = _wrapper(tmp_path)
    tool = subprocess_tool(
        enabled=True, sandbox=SandboxSettings(required=True, wrapper=(str(wrapper),))
    )
    result = await tool.execute({"executable": "/bin/echo", "arguments": ["hi"]}, _context())
    assert result["exit_code"] == 0 and result["stdout"].strip() == "hi"
    assert (tmp_path / "wrapped").read_text().strip() == "yes"


async def test_subprocess_tool_inherits_nothing_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_TEST_SECRET", "do-not-leak")
    tool = subprocess_tool(enabled=True)
    result = await tool.execute({"executable": "/usr/bin/env"}, _context())
    assert "SANDBOX_TEST_SECRET" not in result["stdout"]


async def test_subprocess_tool_times_out() -> None:
    tool = subprocess_tool(enabled=True, sandbox=SandboxSettings(subprocess_timeout_seconds=0.5))
    with pytest.raises(ToolExecutionError, match="timed out"):
        await tool.execute({"executable": "/bin/sleep", "arguments": ["30"]}, _context())


def test_settings_accept_a_sandbox_policy() -> None:
    settings = AppSettings.model_validate(
        {
            "security": {
                "sandbox": {
                    "required": True,
                    "wrapper": ["bwrap", "--unshare-all", "--die-with-parent", "--"],
                    "env_allowlist": ["PATH"],
                    "limits": {"cpu_seconds": 60, "open_files": 256},
                }
            }
        }
    )
    assert settings.security.sandbox.limits.open_files == 256


async def test_stdio_mcp_refuses_without_a_wrapper_before_importing_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "mcp", None)
    manager = MCPClientManager(
        (MCPStdioServerConfig(name="local", command="server"),),
        security=SecuritySettings(sandbox=SandboxSettings(required=True)),
    )
    with pytest.raises(PolicyDeniedError, match="no sandbox wrapper"):
        await manager.connect("local")


async def test_stdio_mcp_server_runs_inside_the_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("mcp")
    monkeypatch.setenv("SANDBOX_TEST_SECRET", "do-not-leak")
    server = tmp_path / "server.py"
    server.write_text(
        "import json, os, resource\n"
        "from mcp.server.fastmcp import FastMCP\n"
        "mcp = FastMCP('sandboxed')\n"
        "@mcp.tool()\n"
        "def report() -> str:\n"
        "    return json.dumps({'env': sorted(os.environ),\n"
        "        'files': resource.getrlimit(resource.RLIMIT_NOFILE)[0]})\n"
        "if __name__ == '__main__':\n"
        "    mcp.run(transport='stdio')\n"
    )
    wrapper = _wrapper(tmp_path)
    manager = MCPClientManager(
        (
            MCPStdioServerConfig(
                name="sandboxed",
                command=sys.executable,
                args=[str(server)],
                env={"SERVER_SETTING": "1"},
                sandbox=SubprocessSandbox(
                    env_allowlist=("PATH",), limits={"open_files": 128}, wrapper=(str(wrapper),)
                ),
            ),
        ),
        security=SecuritySettings(sandbox=SandboxSettings(required=True)),
    )
    try:
        tools = await manager.discover_tools()
        result = await tools[0].execute({}, _context())
        report = json.loads(result["text"])
    finally:
        await manager.close()
    assert "SANDBOX_TEST_SECRET" not in report["env"]
    assert "HOME" not in report["env"]
    assert "SERVER_SETTING" in report["env"]
    assert report["files"] == 128
    assert (tmp_path / "wrapped").exists()

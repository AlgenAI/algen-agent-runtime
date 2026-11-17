"""Cancelling a run while one of its tool calls runs: what stops and what cannot.

- An async tool, an HTTP tool and an MCP tool are cancelled where they wait: the handler sees
  ``CancelledError``, an HTTP request is aborted (the server sees the client hang up).
- A tool with ``execution="process"`` runs in a subprocess, which is killed.
- A synchronous tool in a worker thread cannot be stopped: the run ends ``cancelled`` at once,
  but the thread runs to its end, and whatever it does still happens. Nothing it returns is
  recorded, and the call stays ``indeterminate`` for an operator to reconcile when it changes
  things.

In every case the run ends ``cancelled`` with one ``run.cancelled`` event, last; the tool call
never completes in the run's events; and the model is not called again.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import process_tool_handlers as handlers
import pytest
from conftest import make_agent, make_runtime
from fake_model_server import FakeModelServer, tracked_client

from algen_agent_runtime.mcp.client import MCPClientManager
from algen_agent_runtime.mcp.contracts import MCPStdioServerConfig
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.persistence.tool_executions import ToolExecutionStatus
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)
from algen_agent_runtime.tools.http_api import HttpToolConfig, http_api_tool
from algen_agent_runtime.types.contracts import ApprovalPolicy, RunRequest, RunStatus

QUIET_SECONDS = 0.3


def _definition(name: str, **values: Any) -> ToolDefinition:
    values.setdefault("side_effect", SideEffect.WRITE)
    values.setdefault("idempotency", Idempotency.KEYED)
    return ToolDefinition(
        name=name,
        version="1",
        description="A slow tool.",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        timeout_seconds=30,
        **values,
    )


def _runtime(tool: Tool, arguments: dict[str, Any] | None = None) -> tuple[AgentRuntime, Any]:
    model = MockModelProvider([tool_call_response(tool.definition.name, arguments or {}), "done"])
    runtime = make_runtime(
        model,
        make_agent(
            enabled_tools=frozenset({tool.definition.name}),
            tool_permissions=tool.definition.required_permissions,
            # The call runs at once: approvals are covered elsewhere.
            approval_policy=ApprovalPolicy(
                require_for_side_effects=False, require_for_destructive=False
            ),
        ),
    )
    runtime.tools.register(tool)
    return runtime, model


async def _cancel_while(runtime: AgentRuntime, started: Any) -> str:
    """Start a run, wait until ``started()`` is true, cancel it; return its id."""
    state = await runtime.start(
        RunRequest(agent="test-agent", input="go", tenant_id="t", user_id="u")
    )
    for _ in range(1000):
        if started():
            break
        await asyncio.sleep(0.01)
    else:
        current = await runtime.status(state.id, "t")
        raise AssertionError(f"the tool never started: {current.status} {current.error}")
    began = time.monotonic()
    await runtime.cancel(state.id, "t")
    result = await runtime.wait(state.id, "t")
    assert result.status == RunStatus.CANCELLED
    assert time.monotonic() - began < 2  # the cancel never waits for the tool to finish
    return state.id


async def _assert_stopped(runtime: AgentRuntime, model: MockModelProvider, run_id: str) -> None:
    await asyncio.sleep(QUIET_SECONDS)
    assert len(model.requests) == 1  # no model call after the cancel
    history = [event.type for event in await runtime.events.history(run_id)]
    assert history.count("run.cancelled") == 1
    assert history[-1] == "run.cancelled"
    assert "tool.started" in history
    assert "tool.completed" not in history
    assert (await runtime.status(run_id, "t")).status == RunStatus.CANCELLED


async def _record(runtime: AgentRuntime, run_id: str) -> Any:
    [record] = await runtime.tool_executor.executions.find(
        "t", statuses=tuple(ToolExecutionStatus), run_id=run_id
    )
    return record


async def test_an_async_tool_is_cancelled_where_it_waits() -> None:
    seen: list[str] = []
    entered = asyncio.Event()

    async def wait_forever(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            seen.append("cancelled")
            raise
        return {}

    runtime, model = _runtime(Tool(_definition("slow.async"), wait_forever))
    run_id = await _cancel_while(runtime, entered.is_set)

    assert seen == ["cancelled"]
    await _assert_stopped(runtime, model, run_id)
    # A write that was under way may have taken effect: an operator reconciles it.
    assert (await _record(runtime, run_id)).status == ToolExecutionStatus.INDETERMINATE
    await runtime.shutdown(grace_seconds=1)


async def test_a_cancelled_read_tool_call_is_failed_not_indeterminate() -> None:
    entered = asyncio.Event()

    async def wait_forever(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        entered.set()
        await asyncio.Event().wait()
        return {}

    tool = Tool(
        _definition("slow.read", side_effect=SideEffect.READ, idempotency=Idempotency.IDEMPOTENT),
        wait_forever,
    )
    runtime, model = _runtime(tool)
    run_id = await _cancel_while(runtime, entered.is_set)

    await _assert_stopped(runtime, model, run_id)
    assert (await _record(runtime, run_id)).status == ToolExecutionStatus.FAILED
    await runtime.shutdown(grace_seconds=1)


async def test_a_thread_tool_cannot_be_stopped_but_the_run_ends_at_once() -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        entered.set()
        release.wait(10)
        finished.set()  # the side effect, which still happens after the cancel
        return {"done": True}

    runtime, model = _runtime(Tool(_definition("slow.thread"), blocking))
    run_id = await _cancel_while(runtime, entered.is_set)

    # The run is cancelled while its thread is still running.
    assert not finished.is_set()
    release.set()
    await asyncio.to_thread(finished.wait, 5)
    assert finished.is_set()
    await _assert_stopped(runtime, model, run_id)
    # Its result is never recorded: the call stays indeterminate for reconciliation.
    assert (await _record(runtime, run_id)).status == ToolExecutionStatus.INDETERMINATE
    await runtime.shutdown(grace_seconds=1)


async def test_a_process_tool_is_killed(tmp_path: Path) -> None:
    pid_file, done_file = tmp_path / "pid", tmp_path / "done"
    tool = Tool(
        _definition(
            "slow.process",
            execution="process",
            side_effect=SideEffect.EXTERNAL,
            idempotency=Idempotency.NON_IDEMPOTENT,
        ),
        handlers.hang,
    )
    runtime, model = _runtime(tool, {"pid_file": str(pid_file), "done_file": str(done_file)})
    run_id = await _cancel_while(runtime, lambda: pid_file.exists() and pid_file.read_text())

    pid = int(pid_file.read_text())
    for _ in range(300):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the cancelled tool's process is still running")
    assert not done_file.exists()
    await _assert_stopped(runtime, model, run_id)
    assert (await _record(runtime, run_id)).status == ToolExecutionStatus.INDETERMINATE
    await runtime.shutdown(grace_seconds=1)


class FakeSecrets:
    async def get(self, reference: str) -> str:
        return "unused"


async def test_an_http_tool_request_is_aborted() -> None:
    client, transport = tracked_client()
    async with FakeModelServer(delay=None) as server:
        tool = http_api_tool(
            HttpToolConfig(
                name="orders.lookup",
                description="Look up an order.",
                url=f"{server.url}/orders",
            ),
            allowed_hosts=("127.0.0.1",),
            allow_private_networks=True,
            secrets=FakeSecrets(),
            client=client,
        )
        runtime, model = _runtime(tool)
        run_id = await _cancel_while(runtime, server.request_received.is_set)

        await asyncio.wait_for(server.last.disconnected.wait(), 2)
        assert transport.open_responses == 0
        await _assert_stopped(runtime, model, run_id)
        assert len(server.exchanges) == 1
        await runtime.shutdown(grace_seconds=1)
    await client.aclose()


class HangingMCPSession:
    """An MCP session whose tool call never answers and records being cancelled."""

    def __init__(self) -> None:
        self.called = asyncio.Event()
        self.cancelled = False

    async def initialize(self) -> None:
        pass

    async def list_tools(self, cursor: str | None = None) -> Any:
        schema = {"type": "object", "properties": {}}
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="wait",
                    description="Wait",
                    inputSchema=schema,
                    outputSchema=None,
                    annotations=None,
                )
            ],
            nextCursor=None,
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.called.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_an_mcp_tool_call_is_cancelled_in_its_session() -> None:
    session = HangingMCPSession()

    async def session_factory(config: Any, stack: Any) -> Any:
        return session

    manager = MCPClientManager(
        servers=(MCPStdioServerConfig(name="slow_srv", command="cmd", timeout_seconds=30),),
        session_factory=session_factory,
    )
    [tool] = await manager.discover_tools()
    runtime, model = _runtime(tool)
    run_id = await _cancel_while(runtime, session.called.is_set)

    assert session.cancelled
    await _assert_stopped(runtime, model, run_id)
    await runtime.shutdown(grace_seconds=1)
    await manager.aclose()

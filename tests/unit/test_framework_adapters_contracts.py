from __future__ import annotations

import asyncio
import sys
from typing import Any, TypedDict

import pytest

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.frameworks import (
    FrameworkAdapterRegistry,
    FrameworkEventType,
    FrameworkRunRequest,
    LangGraphAdapter,
    OpenAIAgentsAdapter,
)
from algen_agent_runtime.orchestration.container import build_container


def _sample_request() -> FrameworkRunRequest:
    return FrameworkRunRequest(
        input="Analyze security logs",
        run_id="run-framework-1",
        tenant_id="tenant-prod",
        user_id="analyst-42",
        session_id="session-99",
    )


class MockGraph:
    def __init__(self) -> None:
        self.recorded_config: dict[str, Any] = {}

    async def ainvoke(self, value: Any, *, config: dict[str, Any]) -> Any:
        self.recorded_config = config
        return {"output": f"processed: {value['messages'][-1]['content']}"}

    async def astream(self, value: Any, **kwargs: Any) -> Any:
        yield {"step": "analyzing"}
        yield {"output": "analysis complete"}


def test_framework_adapter_registry_api() -> None:
    registry = FrameworkAdapterRegistry()
    assert not registry.has("langgraph")
    assert registry.list() == ()

    adapter = LangGraphAdapter(MockGraph())
    registry.register(adapter)

    assert registry.has("langgraph")
    assert registry.list() == ("langgraph",)
    assert registry.get("langgraph") is adapter
    assert "langgraph" in registry.capabilities()

    with pytest.raises(ValueError, match=r"already registered"):
        registry.register(adapter)

    with pytest.raises(KeyError, match=r"not registered"):
        registry.get("unknown_framework")


def test_container_framework_wiring() -> None:
    settings = AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
            "agents": [
                {
                    "name": "agent-1",
                    "version": "1.0.0",
                    "description": "test",
                    "system_instructions": "test",
                    "default_model": {"name": "default", "provider": "mock", "model": "m"},
                }
            ],
        }
    )
    adapter = LangGraphAdapter(MockGraph())
    container = build_container(settings, framework_adapters=(adapter,))

    assert container.frameworks.has("langgraph")
    assert container.frameworks.get("langgraph") is adapter


@pytest.mark.asyncio
async def test_langgraph_adapter_cancellation() -> None:
    class SlowGraph:
        async def ainvoke(self, value: Any, *, config: dict[str, Any]) -> Any:
            await asyncio.sleep(5.0)
            return {"output": "done"}

    adapter = LangGraphAdapter(SlowGraph())
    req = _sample_request()

    task = asyncio.create_task(adapter.invoke(req))
    await asyncio.sleep(0.05)

    cancelled = await adapter.cancel(req.run_id)
    assert cancelled

    with pytest.raises(asyncio.CancelledError):
        await task


def test_openai_agents_missing_dependency_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # When runner is None and agents is not importable, should raise informative ImportError
    # Force import of 'agents' to fail even if the extra is installed in the test environment
    monkeypatch.setitem(sys.modules, "agents", None)
    with pytest.raises(ImportError, match=r"install algen-agent-runtime\[openai-agents\]"):
        OpenAIAgentsAdapter(object(), runner=None)


# ---------------------------------------------------------------------------
# Real LangGraph Integration Contract Test
# ---------------------------------------------------------------------------


class _RealGraphState(TypedDict):
    input: str
    messages: list[dict[str, str]]
    output: str


@pytest.mark.asyncio
async def test_real_langgraph_execution_and_streaming() -> None:
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        pytest.skip("langgraph package is not installed")

    def process_node(state: _RealGraphState) -> dict[str, Any]:
        # Extract user input message
        user_msg = (
            state["messages"][-1]["content"] if state.get("messages") else state.get("input", "")
        )
        return {"output": f"LangGraph executed: {user_msg}"}

    builder = StateGraph(_RealGraphState)
    builder.add_node("process", process_node)
    builder.add_edge(START, "process")
    builder.add_edge("process", END)
    compiled_graph = builder.compile()

    adapter = LangGraphAdapter(compiled_graph)

    # 1. Test invocation
    req = _sample_request()
    result = await adapter.invoke(req)

    assert result.status.value == "completed"
    assert result.output == "LangGraph executed: Analyze security logs"

    # 2. Test streaming
    events = [event async for event in adapter.stream(req)]
    assert len(events) >= 2
    assert events[0].type == FrameworkEventType.STARTED
    assert events[-1].type == FrameworkEventType.COMPLETED
    assert events[-1].result is not None
    assert events[-1].result.output == "LangGraph executed: Analyze security logs"


# ---------------------------------------------------------------------------
# Real OpenAI Agents SDK Integration Contract Test Suite (A3-11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_openai_agents_invoke_and_metadata_propagation() -> None:
    pytest.importorskip("agents")
    from agents import Agent
    from agents.testing import ScriptedModel, assistant_message

    model = ScriptedModel([[assistant_message("Log analysis completed successfully")]])
    agent = Agent(name="security-agent", model=model)
    adapter = OpenAIAgentsAdapter(agent)

    req = _sample_request()
    result = await adapter.invoke(req)

    assert result.status.value == "completed"
    assert result.output == "Log analysis completed successfully"
    assert result.usage is not None
    assert result.resume_state is None
    assert adapter.capabilities.persistence is False
    assert adapter.capabilities.cancellation is True
    assert adapter.capabilities.streaming is True
    model.assert_complete()


@pytest.mark.asyncio
async def test_real_openai_agents_two_turn_tool_loop() -> None:
    pytest.importorskip("agents")
    from agents import Agent, function_tool
    from agents.testing import ScriptedModel, assistant_message, function_call

    @function_tool
    def query_audit_log(service: str) -> str:
        return f"audit-record-for-{service}"

    model = ScriptedModel(
        [
            [
                function_call(
                    name="query_audit_log", arguments={"service": "auth"}, call_id="call-42"
                )
            ],
            [assistant_message("Audit record found for auth service.")],
        ]
    )
    agent = Agent(name="tool-agent", model=model, tools=[query_audit_log])
    adapter = OpenAIAgentsAdapter(agent)

    req = _sample_request()
    result = await adapter.invoke(req)

    assert result.status.value == "completed"
    assert result.output == "Audit record found for auth service."
    model.assert_complete()


@pytest.mark.asyncio
async def test_real_openai_agents_streaming() -> None:
    pytest.importorskip("agents")
    from agents import Agent
    from agents.testing import ScriptedModel, assistant_message

    model = ScriptedModel([[assistant_message("streaming log response")]])
    agent = Agent(name="stream-agent", model=model)
    adapter = OpenAIAgentsAdapter(agent)

    req = _sample_request()
    events = [e async for e in adapter.stream(req)]

    assert len(events) >= 3
    assert events[0].type == FrameworkEventType.STARTED
    types = {e.type for e in events}
    assert FrameworkEventType.DELTA in types or FrameworkEventType.STEP in types
    assert events[-1].type == FrameworkEventType.COMPLETED
    assert events[-1].result is not None
    assert events[-1].result.output == "streaming log response"
    model.assert_complete()


@pytest.mark.asyncio
async def test_real_openai_agents_cancellation_clears_active() -> None:
    pytest.importorskip("agents")
    from agents import Agent
    from agents.testing import ScriptedModel, assistant_message

    model = ScriptedModel([[assistant_message("done")]])
    agent = Agent(name="cancel-agent", model=model)
    adapter = OpenAIAgentsAdapter(agent)

    orig_run = adapter._runner.run

    async def slow_run(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(5.0)
        return await orig_run(*args, **kwargs)

    adapter._runner.run = slow_run

    req = _sample_request()
    task = asyncio.create_task(adapter.invoke(req))
    await asyncio.sleep(0.05)

    assert req.run_id in adapter._active
    cancelled = await adapter.cancel(req.run_id)
    assert cancelled is True
    assert req.run_id not in adapter._active

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_real_openai_agents_interruption_normalization() -> None:
    pytest.importorskip("agents")
    from types import SimpleNamespace

    from agents import Agent

    class InterruptedRunner:
        @staticmethod
        async def run(agent: Any, value: str, **kwargs: Any) -> Any:
            return SimpleNamespace(
                final_output="",
                interruptions=[
                    SimpleNamespace(type="tool_approval_item", tool_name="sensitive_action")
                ],
                context_wrapper=SimpleNamespace(usage=None),
            )

    agent = Agent(name="interrupted-agent", model="mock-model")
    adapter = OpenAIAgentsAdapter(agent, runner=InterruptedRunner)

    req = _sample_request()
    result = await adapter.invoke(req)

    assert result.status.value == "awaiting_input"
    assert result.resume_state is None
    assert result.metadata["interruption_count"] == 1
    assert result.metadata["interruptions"] == [
        {"type": "tool_approval_item", "tool_name": "sensitive_action"}
    ]


@pytest.mark.asyncio
async def test_real_openai_agents_runtime_metadata_boundary() -> None:
    pytest.importorskip("agents")
    from types import SimpleNamespace

    from agents import Agent
    from agents.testing import ScriptedModel, assistant_message

    captured_kwargs: dict[str, Any] = {}

    class InspectingRunner:
        @staticmethod
        async def run(agent: Any, value: str, **kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            return SimpleNamespace(
                final_output="ok",
                interruptions=(),
                context_wrapper=SimpleNamespace(usage=None),
            )

    agent = Agent(name="meta-agent", model=ScriptedModel([[assistant_message("ok")]]))
    adapter = OpenAIAgentsAdapter(agent, runner=InspectingRunner)

    req = _sample_request()
    await adapter.invoke(req)

    assert captured_kwargs["context"]["run_id"] == "run-framework-1"
    assert captured_kwargs["context"]["tenant_id"] == "tenant-prod"
    assert captured_kwargs["context"]["user_id"] == "analyst-42"
    assert captured_kwargs["context"]["session_id"] == "session-99"
    assert captured_kwargs["conversation_id"] == "session-99"
    assert captured_kwargs["run_config"].tracing_disabled is True
    assert captured_kwargs["run_config"].trace_id == "run-framework-1"

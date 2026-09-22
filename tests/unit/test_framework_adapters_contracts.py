from __future__ import annotations

import asyncio
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


def test_openai_agents_missing_dependency_error() -> None:
    # When runner is None and agents is not importable, should raise informative ImportError
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

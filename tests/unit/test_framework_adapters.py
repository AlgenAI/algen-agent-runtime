from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from algen_agent_runtime.frameworks import (
    AutoGenAdapter,
    CrewAIAdapter,
    FrameworkAdapterRegistry,
    FrameworkEventType,
    FrameworkRunRequest,
    LangGraphAdapter,
    OpenAIAgentsAdapter,
)


def request() -> FrameworkRunRequest:
    return FrameworkRunRequest(
        input="hello",
        run_id="run-1",
        tenant_id="tenant-a",
        user_id="user-1",
        session_id="session-1",
    )


class FakeGraph:
    def __init__(self) -> None:
        self.config: dict[str, Any] = {}

    async def ainvoke(self, value: Any, *, config: dict[str, Any]) -> Any:
        self.config = config
        return {"messages": [*value["messages"], {"role": "assistant", "content": "graph"}]}

    async def astream(self, value: Any, **kwargs: Any) -> Any:
        del value, kwargs
        yield {"node": {"answer": "graph"}}


async def test_langgraph_adapter_propagates_identity_and_normalizes_output() -> None:
    graph = FakeGraph()
    result = await LangGraphAdapter(graph).invoke(request())

    assert result.output == "graph"
    assert graph.config["configurable"]["thread_id"] == "session-1"
    assert graph.config["metadata"]["tenant_id"] == "tenant-a"


class FakeRunner:
    @staticmethod
    async def run(agent: Any, value: str, **kwargs: Any) -> Any:
        del agent, value, kwargs
        usage = SimpleNamespace(input_tokens=4, output_tokens=2)
        return SimpleNamespace(
            final_output="openai",
            interruptions=(),
            context_wrapper=SimpleNamespace(usage=usage),
        )

    @staticmethod
    def run_streamed(agent: Any, value: str, **kwargs: Any) -> Any:
        del agent, value, kwargs

        class Stream:
            final_output = "openai"

            async def stream_events(self) -> Any:
                yield SimpleNamespace(
                    type="raw_response_event", data=SimpleNamespace(delta="token")
                )

        return Stream()


async def test_openai_agents_adapter_normalizes_usage_and_stream() -> None:
    adapter = OpenAIAgentsAdapter(object(), runner=FakeRunner)
    result = await adapter.invoke(request())
    events = [event async for event in adapter.stream(request())]

    assert result.output == "openai"
    assert result.usage.total_tokens == 6
    assert events[1].type == FrameworkEventType.DELTA
    assert events[1].delta == "token"
    assert events[-1].result and events[-1].result.output == "openai"


class FakeAutoGen:
    @staticmethod
    async def run(*, task: str) -> Any:
        del task
        usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2)
        return SimpleNamespace(
            messages=[SimpleNamespace(content="autogen", models_usage=usage)],
            stop_reason="done",
        )

    @staticmethod
    async def run_stream(*, task: str) -> Any:
        del task
        yield SimpleNamespace(content="auto", source="assistant")
        yield SimpleNamespace(messages=[SimpleNamespace(content="autogen")])


async def test_autogen_adapter_accepts_agents_and_teams() -> None:
    result = await AutoGenAdapter(FakeAutoGen()).invoke(request())
    assert result.output == "autogen"
    assert result.usage.total_tokens == 5


class FakeCrew:
    @staticmethod
    async def kickoff_async(*, inputs: dict[str, Any]) -> Any:
        assert inputs == {"input": "hello"}
        return SimpleNamespace(
            raw="crew",
            token_usage={"prompt_tokens": 5, "completion_tokens": 1},
        )


async def test_crewai_adapter_and_registry() -> None:
    adapter = CrewAIAdapter(FakeCrew())
    registry = FrameworkAdapterRegistry()
    registry.register(adapter)

    result = await registry.get("crewai").invoke(request())
    assert result.output == "crew"
    assert result.usage.total_tokens == 6
    assert registry.capabilities()["crewai"].multi_agent

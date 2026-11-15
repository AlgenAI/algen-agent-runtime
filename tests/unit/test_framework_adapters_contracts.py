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

    # Since 0.2.0b4 a run stopped by cancel() returns CANCELLED instead of raising.
    result = await task
    assert result.status.value == "cancelled"


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
async def test_real_openai_agents_cancellation_clears_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    # The adapter holds the SDK class Runner: patch it for this test only.
    monkeypatch.setattr(adapter._runner, "run", slow_run)

    req = _sample_request()
    task = asyncio.create_task(adapter.invoke(req))
    await asyncio.sleep(0.05)

    assert req.run_id in adapter._active
    cancelled = await adapter.cancel(req.run_id)
    assert cancelled is True
    assert req.run_id not in adapter._active

    result = await task
    assert result.status.value == "cancelled"


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
    # The RunResult is kept so the host can resume it (to_state, approve/reject).
    assert result.resume_state is not None
    assert result.metadata["interruption_count"] == 1
    assert result.metadata["interruptions"] == [
        {
            "type": "tool_approval_item",
            "tool_name": "sensitive_action",
            "call_id": None,
            "arguments": None,
        }
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
    assert "conversation_id" not in captured_kwargs
    assert captured_kwargs["run_config"].tracing_disabled is True
    assert captured_kwargs["run_config"].trace_id == "run-framework-1"


class _ApprovalState(TypedDict):
    decision: str


@pytest.mark.asyncio
async def test_real_langgraph_interrupt_is_awaiting_input_and_resumes_on_the_tenant_thread() -> (
    None
):
    pytest.importorskip("langgraph")
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt

    def review(state: _ApprovalState) -> dict[str, Any]:
        return {"decision": str(interrupt({"action": "refund", "amount": 40}))}

    builder = StateGraph(_ApprovalState)
    builder.add_node("review", review)
    builder.add_edge(START, "review")
    builder.add_edge("review", END)
    saver = InMemorySaver()
    graph = builder.compile(checkpointer=saver)
    resuming = {"value": None}

    def input_factory(request: FrameworkRunRequest) -> Any:
        if resuming["value"] is not None:
            return Command(resume=resuming["value"])
        return {"decision": ""}

    adapter = LangGraphAdapter(
        graph, input_factory=input_factory, output_selector=lambda value: value["decision"]
    )
    req = _sample_request()

    paused = await adapter.invoke(req)
    streamed = [e async for e in adapter.stream(req.model_copy(update={"session_id": "other"}))]
    resuming["value"] = "approved"
    resumed = await adapter.invoke(req)

    assert adapter.capabilities.persistence is True
    assert paused.status.value == "awaiting_input"
    [interrupt_item] = paused.metadata["interrupts"]
    assert interrupt_item["value"] == {"action": "refund", "amount": 40}
    assert interrupt_item["id"]
    assert streamed[-1].result is not None
    assert streamed[-1].result.status.value == "awaiting_input"
    assert len(streamed[-1].result.metadata["interrupts"]) == 1
    assert resumed.status.value == "completed"
    assert resumed.output == "approved"
    threads = {item.config["configurable"]["thread_id"] for item in saver.list(None)}
    assert threads == {"tenant-prod:session-99", "tenant-prod:other"}


@pytest.mark.asyncio
async def test_real_openai_agents_tool_approval_pauses_with_arguments_and_resumes() -> None:
    pytest.importorskip("agents")
    from agents import Agent, RunConfig, Runner, function_tool
    from agents.testing import ScriptedModel, assistant_message, function_call

    @function_tool(needs_approval=True)
    def issue_refund(amount: int) -> str:
        """Refund an order."""
        return f"refunded {amount}"

    model = ScriptedModel(
        [
            [function_call("issue_refund", {"amount": 40}, call_id="call-1")],
            [assistant_message("refund issued")],
        ]
    )
    agent = Agent(name="refunds", model=model, tools=[issue_refund])

    paused = await OpenAIAgentsAdapter(agent).invoke(_sample_request())

    assert paused.status.value == "awaiting_input"
    assert paused.metadata["interrupts"] == [
        {"id": "call-1", "value": {"tool_name": "issue_refund", "arguments": {"amount": 40}}}
    ]
    # The kept RunResult resumes the run with the SDK's own approval API.
    state = paused.resume_state.to_state()
    state.approve(paused.resume_state.interruptions[0])
    resumed = await Runner.run(agent, state, run_config=RunConfig(tracing_disabled=True))
    assert resumed.final_output == "refund issued"


@pytest.mark.asyncio
async def test_real_openai_agents_stream_reports_a_tool_approval() -> None:
    pytest.importorskip("agents")
    from agents import Agent, function_tool
    from agents.testing import ScriptedModel, function_call

    @function_tool(needs_approval=True)
    def issue_refund(amount: int) -> str:
        """Refund an order."""
        return f"refunded {amount}"

    model = ScriptedModel([[function_call("issue_refund", {"amount": 5}, call_id="call-2")]])
    agent = Agent(name="refunds", model=model, tools=[issue_refund])

    events = [e async for e in OpenAIAgentsAdapter(agent).stream(_sample_request())]

    assert events[-1].type == FrameworkEventType.COMPLETED
    assert events[-1].result is not None
    assert events[-1].result.status.value == "awaiting_input"
    assert events[-1].result.metadata["interrupts"][0]["id"] == "call-2"


@pytest.mark.asyncio
async def test_real_autogen_team_isolation_handoff_and_cancellation() -> None:
    pytest.importorskip("autogen_agentchat")
    from autogen_agentchat.agents import BaseChatAgent
    from autogen_agentchat.base import Response
    from autogen_agentchat.conditions import HandoffTermination, MaxMessageTermination
    from autogen_agentchat.messages import HandoffMessage, TextMessage
    from autogen_agentchat.teams import RoundRobinGroupChat

    from algen_agent_runtime.frameworks import AutoGenAdapter

    class Counter(BaseChatAgent):
        """Answers with how many messages it has seen, which grows unless the team is reset."""

        def __init__(self, *, handoff: bool = False, slow: bool = False) -> None:
            super().__init__("counter", "counts messages")
            self.seen = 0
            self.handoff = handoff
            self.slow = slow

        @property
        def produced_message_types(self) -> Any:
            return (TextMessage, HandoffMessage)

        async def on_messages(self, messages: Any, cancellation_token: Any) -> Response:
            self.seen += len(messages)
            if self.slow:
                # Stops only through the CancellationToken the adapter passes to team.run.
                waiting = asyncio.ensure_future(asyncio.sleep(30))
                cancellation_token.link_future(waiting)
                await waiting
            if self.handoff:
                message: Any = HandoffMessage(source=self.name, target="user", content="ok?")
            else:
                message = TextMessage(source=self.name, content=f"seen {self.seen}")
            return Response(chat_message=message)

        async def on_reset(self, cancellation_token: Any) -> None:
            self.seen = 0

    def request(tenant: str, run_id: str = "run-1") -> FrameworkRunRequest:
        return FrameworkRunRequest(
            input="hi", run_id=run_id, tenant_id=tenant, user_id="u", session_id="s"
        )

    shared = AutoGenAdapter(
        RoundRobinGroupChat([Counter()], termination_condition=MaxMessageTermination(2))
    )
    first = await shared.invoke(request("tenant-a"))
    second = await shared.invoke(request("tenant-b"))
    assert (first.output, second.output) == ("seen 1", "seen 1")

    handoff_team = RoundRobinGroupChat(
        [Counter(handoff=True)], termination_condition=HandoffTermination(target="user")
    )
    paused = await AutoGenAdapter(handoff_team).invoke(request("tenant-a"))
    assert paused.status.value == "awaiting_input"
    assert paused.metadata["interrupts"][0]["value"]["target"] == "user"

    slow = AutoGenAdapter(
        RoundRobinGroupChat([Counter(slow=True)], termination_condition=MaxMessageTermination(2))
    )
    task = asyncio.create_task(slow.invoke(request("tenant-a", "run-slow")))
    await asyncio.sleep(0.2)
    assert await slow.cancel("run-slow") is True
    assert (await task).status.value == "cancelled"

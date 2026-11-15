"""Every framework adapter keeps the promises its capability flags make (#146).

Fake framework objects only, so this runs in the base environment without the framework extras:

- ``human_in_the_loop``: a pause is reported as ``AWAITING_INPUT``, with the interrupt's id and
  structured value, by ``invoke`` and by ``stream``;
- ``cancellation``: ``cancel`` stops ``invoke`` and ``stream`` (the framework's work is
  cancelled, and the result is ``CANCELLED``);
- persistence and isolation: two tenants or sessions never share a thread, conversation or state;
- a framework exception is a ``FAILED`` result, never a raw exception;
- the run's identity comes from the request and ``request.context`` cannot override it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from algen_agent_runtime.frameworks import (
    AutoGenAdapter,
    CrewAIAdapter,
    FrameworkEventType,
    FrameworkRunRequest,
    FrameworkRunResult,
    FrameworkRunStatus,
    FrameworkStreamEvent,
    LangGraphAdapter,
    OpenAIAgentsAdapter,
)


def _request(
    *, tenant: str = "tenant-a", session: str = "session-1", run: str = "run-1", **context: Any
) -> FrameworkRunRequest:
    return FrameworkRunRequest(
        input="refund order 42",
        run_id=run,
        tenant_id=tenant,
        user_id="user-1",
        session_id=session,
        context=context,
    )


async def _events(adapter: Any, request: FrameworkRunRequest) -> list[FrameworkStreamEvent]:
    return [event async for event in adapter.stream(request)]


def _final(events: list[FrameworkStreamEvent]) -> FrameworkRunResult:
    assert events[0].type == FrameworkEventType.STARTED
    assert events[-1].type in {FrameworkEventType.COMPLETED, FrameworkEventType.FAILED}
    assert events[-1].result is not None
    return events[-1].result


class Gate:
    """Lets a fake framework call block until the test cancels it, and records that it was."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = False

    async def wait(self) -> None:
        self.entered.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


# --------------------------------------------------------------------------------------------
# Fake frameworks
# --------------------------------------------------------------------------------------------


class Interrupt:
    """LangGraph's ``Interrupt``: a value and a stable id."""

    def __init__(self, value: Any, id: str) -> None:
        self.value = value
        self.id = id


class FakeGraph:
    """A compiled graph: ``ainvoke`` returns LangGraph 1.x's default (v1) output, and ``astream``
    honours ``stream_mode=["updates", "values"]``."""

    def __init__(
        self,
        *,
        interrupt: bool = False,
        error: Exception | None = None,
        gate: Gate | None = None,
        checkpointer: Any = None,
    ) -> None:
        self.interrupt = interrupt
        self.error = error
        self.gate = gate
        self.checkpointer = checkpointer
        self.configs: list[dict[str, Any]] = []

    def _state(self) -> dict[str, Any]:
        return {
            "messages": [
                {"role": "user", "content": "refund order 42"},
                {
                    "role": "assistant",
                    "content": "draft refund",
                    "id": "m-2",
                    "usage_metadata": {"input_tokens": 7, "output_tokens": 3},
                },
            ]
        }

    def _interrupts(self) -> tuple[Interrupt, ...]:
        return (Interrupt({"action": "refund", "amount": 40}, "int-1"),)

    async def ainvoke(self, value: Any, *, config: dict[str, Any]) -> Any:
        self.configs.append(config)
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        state = self._state()
        if self.interrupt:
            state["__interrupt__"] = self._interrupts()
        return state

    async def astream(self, value: Any, *, config: dict[str, Any], stream_mode: Any) -> Any:
        self.configs.append(config)
        assert stream_mode == ["updates", "values"]
        yield ("updates", {"draft": {"messages": ["draft refund"]}})
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        if self.interrupt:
            # As LangGraph 1.x does: the interrupt in an updates chunk, then again in the values.
            yield ("updates", {"__interrupt__": self._interrupts()})
            yield ("values", {**self._state(), "__interrupt__": self._interrupts()})
            return
        yield ("values", self._state())


class V2Output:
    """LangGraph's ``version="v2"`` output: ``.value`` and ``.interrupts``."""

    def __init__(self, value: Any, interrupts: tuple[Any, ...]) -> None:
        self.value = value
        self.interrupts = interrupts


class FakeRunner:
    """The OpenAI Agents SDK ``Runner``."""

    def __init__(
        self,
        *,
        interrupt: bool = False,
        error: Exception | None = None,
        gate: Gate | None = None,
    ) -> None:
        self.interrupt = interrupt
        self.error = error
        self.gate = gate
        self.kwargs: list[dict[str, Any]] = []

    def _interruptions(self) -> list[Any]:
        if not self.interrupt:
            return []
        raw = SimpleNamespace(call_id="call-9", name="issue_refund", arguments='{"amount": 40}')
        return [SimpleNamespace(type="tool_approval_item", tool_name="issue_refund", raw_item=raw)]

    def _result(self) -> Any:
        return SimpleNamespace(
            final_output="refund drafted",
            interruptions=self._interruptions(),
            context_wrapper=SimpleNamespace(usage=SimpleNamespace(input_tokens=4, output_tokens=2)),
        )

    async def run(self, agent: Any, value: str, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        return self._result()

    def run_streamed(self, agent: Any, value: str, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        runner = self
        result = self._result()

        class Streamed:
            final_output = result.final_output
            interruptions = result.interruptions
            context_wrapper = result.context_wrapper
            stopped = False

            async def stream_events(self) -> AsyncIterator[Any]:
                yield SimpleNamespace(type="raw_response_event", data=SimpleNamespace(delta="ok"))
                if runner.gate:
                    await runner.gate.wait()
                if runner.error:
                    raise runner.error

            def cancel(self) -> None:
                Streamed.stopped = True

        return Streamed()


class HandoffMessage:
    def __init__(self, target: str) -> None:
        self.id = "handoff-1"
        self.source = "refund_agent"
        self.target = target
        self.content = "Please confirm the refund."
        self.models_usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)


class FakeTeam:
    """An AutoGen team that, like a real one, keeps its conversation between ``run`` calls
    until ``reset()``."""

    def __init__(
        self,
        *,
        handoff_to: str | None = None,
        error: Exception | None = None,
        gate: Gate | None = None,
    ) -> None:
        self.history: list[str] = []
        self.handoff_to = handoff_to
        self.error = error
        self.gate = gate
        self.resets = 0
        self.running = 0
        self.max_running = 0

    async def reset(self) -> None:
        self.history.clear()
        self.resets += 1

    def _result(self) -> Any:
        usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2)
        seen = " | ".join(self.history)
        messages: list[Any] = [SimpleNamespace(content=f"seen: {seen}", models_usage=usage)]
        if self.handoff_to:
            messages.append(HandoffMessage(self.handoff_to))
        return SimpleNamespace(messages=messages, stop_reason="done")

    async def run(self, *, task: str) -> Any:
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            self.history.append(task)
            await asyncio.sleep(0)
            if self.gate:
                await self.gate.wait()
            if self.error:
                raise self.error
            return self._result()
        finally:
            self.running -= 1

    async def run_stream(self, *, task: str) -> AsyncIterator[Any]:
        self.history.append(task)
        yield SimpleNamespace(content="thinking", source="refund_agent")
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        yield self._result()


class FakeCrew:
    def __init__(
        self,
        *,
        asynchronous: bool = True,
        error: Exception | None = None,
        gate: Gate | None = None,
    ) -> None:
        self.error = error
        self.gate = gate
        if asynchronous:
            self.akickoff = self._akickoff

    def _output(self) -> Any:
        return SimpleNamespace(
            raw="refund approved",
            pydantic=None,
            json_dict={"refund": 40, "currency": "EUR"},
            token_usage={"prompt_tokens": 5, "completion_tokens": 1},
        )

    async def _akickoff(self, *, inputs: dict[str, Any]) -> Any:
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        return self._output()

    def kickoff(self, *, inputs: dict[str, Any]) -> Any:
        if self.error:
            raise self.error
        return self._output()


# Each adapter with a fake framework that waits at a gate, or raises.
Factory = Callable[..., Any]

ADAPTERS: dict[str, Factory] = {
    "langgraph": lambda **kw: LangGraphAdapter(FakeGraph(**kw)),
    "openai_agents": lambda **kw: OpenAIAgentsAdapter(object(), runner=FakeRunner(**kw)),
    "autogen": lambda **kw: AutoGenAdapter(FakeTeam(**kw)),
    "crewai": lambda **kw: CrewAIAdapter(FakeCrew(**kw)),
}


# --------------------------------------------------------------------------------------------
# Cross-adapter conformance
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ADAPTERS))
async def test_a_framework_exception_is_a_failed_result_with_a_redacted_error(name: str) -> None:
    error = RuntimeError("upstream refused: api_key=sk-livesecretvalue123")
    adapter = ADAPTERS[name](error=error)

    result = await adapter.invoke(_request())
    streamed = _final(await _events(ADAPTERS[name](error=error), _request()))

    for outcome in (result, streamed):
        assert outcome.status == FrameworkRunStatus.FAILED
        assert outcome.metadata["error"]["type"] == "RuntimeError"
        assert "sk-livesecretvalue123" not in outcome.metadata["error"]["message"]
        assert "upstream refused" in outcome.metadata["error"]["message"]


@pytest.mark.parametrize("name", sorted(ADAPTERS))
async def test_cancellation_stops_invoke_when_advertised(name: str) -> None:
    gate = Gate()
    adapter = ADAPTERS[name](gate=gate)
    if not adapter.capabilities.cancellation:
        pytest.skip(f"{name} does not advertise cancellation")
    request = _request()

    task = asyncio.create_task(adapter.invoke(request))
    await asyncio.wait_for(gate.entered.wait(), 5)

    assert await adapter.cancel(request.run_id) is True
    result = await asyncio.wait_for(task, 5)

    assert result.status == FrameworkRunStatus.CANCELLED
    assert gate.cancelled
    assert await adapter.cancel(request.run_id) is False


@pytest.mark.parametrize("name", sorted(ADAPTERS))
async def test_cancellation_stops_stream_when_advertised(name: str) -> None:
    gate = Gate()
    adapter = ADAPTERS[name](gate=gate)
    if not adapter.capabilities.cancellation:
        pytest.skip(f"{name} does not advertise cancellation")
    request = _request()

    task = asyncio.create_task(_events(adapter, request))
    await asyncio.wait_for(gate.entered.wait(), 5)

    assert await adapter.cancel(request.run_id) is True
    events = await asyncio.wait_for(task, 5)

    assert _final(events).status == FrameworkRunStatus.CANCELLED
    assert events[-1].type == FrameworkEventType.FAILED
    assert gate.cancelled


@pytest.mark.parametrize("name", sorted(ADAPTERS))
async def test_cancelling_the_callers_own_task_is_still_raised(name: str) -> None:
    gate = Gate()
    adapter = ADAPTERS[name](gate=gate)
    task = asyncio.create_task(adapter.invoke(_request()))
    await asyncio.wait_for(gate.entered.wait(), 5)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("name", sorted(ADAPTERS))
async def test_stream_and_invoke_report_the_same_output(name: str) -> None:
    invoked = await ADAPTERS[name]().invoke(_request())
    streamed = _final(await _events(ADAPTERS[name](), _request()))

    assert invoked.status == streamed.status == FrameworkRunStatus.COMPLETED
    assert invoked.output == streamed.output
    assert invoked.usage == streamed.usage


@pytest.mark.parametrize("name", sorted(ADAPTERS))
async def test_human_in_the_loop_adapters_report_a_pause(name: str) -> None:
    adapter = ADAPTERS[name]()
    if not adapter.capabilities.human_in_the_loop:
        pytest.skip(f"{name} does not advertise human_in_the_loop")
    pausing = {
        "langgraph": lambda: LangGraphAdapter(FakeGraph(interrupt=True)),
        "openai_agents": lambda: OpenAIAgentsAdapter(object(), runner=FakeRunner(interrupt=True)),
    }[name]

    result = await pausing().invoke(_request())
    streamed = _final(await _events(pausing(), _request()))

    for outcome in (result, streamed):
        assert outcome.status == FrameworkRunStatus.AWAITING_INPUT
        assert outcome.metadata["interrupts"][0]["id"]
        assert isinstance(outcome.metadata["interrupts"][0]["value"], dict)
    assert result.resume_state is not None


# --------------------------------------------------------------------------------------------
# LangGraph
# --------------------------------------------------------------------------------------------


async def test_langgraph_detects_an_interrupt_in_the_default_v1_output() -> None:
    result = await LangGraphAdapter(FakeGraph(interrupt=True)).invoke(_request())

    assert result.status == FrameworkRunStatus.AWAITING_INPUT
    # The interrupt keeps its id and its structured value; the old string list stays.
    assert result.metadata["interrupts"] == [
        {"id": "int-1", "value": {"action": "refund", "amount": 40}}
    ]
    assert result.metadata["interruptions"] == ["{'action': 'refund', 'amount': 40}"]
    assert result.output == "draft refund"
    assert result.metadata["checkpointer"] is False


async def test_langgraph_detects_an_interrupt_in_the_v2_output() -> None:
    class Graph(FakeGraph):
        async def ainvoke(self, value: Any, *, config: dict[str, Any]) -> Any:
            return V2Output(self._state(), self._interrupts())

    result = await LangGraphAdapter(Graph()).invoke(_request())

    assert result.status == FrameworkRunStatus.AWAITING_INPUT
    assert result.metadata["interrupts"][0]["id"] == "int-1"


async def test_langgraph_stream_reports_an_interrupt_and_the_final_state() -> None:
    events = await _events(LangGraphAdapter(FakeGraph(interrupt=True)), _request())
    result = _final(events)

    assert result.status == FrameworkRunStatus.AWAITING_INPUT
    assert result.metadata["interrupts"] == [
        {"id": "int-1", "value": {"action": "refund", "amount": 40}}
    ]
    # The output comes from the final state, as invoke's does, not from the last update.
    assert result.output == "draft refund"
    assert [event.type for event in events[1:-1]] == [FrameworkEventType.STEP]


async def test_langgraph_sums_token_usage_from_the_final_messages() -> None:
    result = await LangGraphAdapter(FakeGraph()).invoke(_request())

    assert (result.usage.input_tokens, result.usage.output_tokens) == (7, 3)


async def test_langgraph_threads_are_namespaced_by_tenant() -> None:
    graph = FakeGraph()
    adapter = LangGraphAdapter(graph)

    await adapter.invoke(_request(tenant="tenant-a", session="shared"))
    await adapter.invoke(_request(tenant="tenant-b", session="shared"))

    threads = [config["configurable"]["thread_id"] for config in graph.configs]
    assert threads == ["tenant-a:shared", "tenant-b:shared"]
    assert graph.configs[0]["metadata"]["tenant_id"] == "tenant-a"


async def test_langgraph_refuses_a_thread_chosen_by_the_caller() -> None:
    graph = FakeGraph()
    adapter = LangGraphAdapter(graph)
    hijack = {"configurable": {"thread_id": "tenant-b:their-session"}}

    refused = await adapter.invoke(_request(langgraph_config=hijack))
    same = {"configurable": {"thread_id": "tenant-a:session-1", "checkpoint_ns": ""}}
    allowed = await adapter.invoke(_request(langgraph_config=same))

    assert refused.status == FrameworkRunStatus.FAILED
    assert refused.metadata["error"]["type"] == "PolicyDeniedError"
    assert allowed.status == FrameworkRunStatus.COMPLETED
    assert [config["configurable"]["thread_id"] for config in graph.configs] == [
        "tenant-a:session-1"
    ]


async def test_langgraph_refuses_identity_in_the_callers_metadata() -> None:
    graph = FakeGraph()
    config = {"metadata": {"tenant_id": "tenant-b", "team": "billing"}}

    result = await LangGraphAdapter(graph).invoke(_request(langgraph_config=config))

    assert result.status == FrameworkRunStatus.FAILED
    assert graph.configs == []


async def test_langgraph_persistence_follows_the_checkpointer() -> None:
    assert LangGraphAdapter(FakeGraph()).capabilities.persistence is False
    assert LangGraphAdapter(FakeGraph(checkpointer=object())).capabilities.persistence is True


# --------------------------------------------------------------------------------------------
# OpenAI Agents SDK
# --------------------------------------------------------------------------------------------


async def test_openai_agents_identity_is_authoritative() -> None:
    runner = FakeRunner()
    adapter = OpenAIAgentsAdapter(object(), runner=runner)

    spoofed = await adapter.invoke(_request(tenant_id="tenant-b", user_id="admin"))
    ok = await adapter.invoke(_request(ticket="T-1"))

    assert spoofed.status == FrameworkRunStatus.FAILED
    assert spoofed.metadata["error"]["type"] == "PolicyDeniedError"
    assert ok.status == FrameworkRunStatus.COMPLETED
    [kwargs] = runner.kwargs
    assert kwargs["context"] == {
        "ticket": "T-1",
        "run_id": "run-1",
        "tenant_id": "tenant-a",
        "user_id": "user-1",
        "session_id": "session-1",
    }
    # An OpenAI server conversation id is never made up from the session id.
    assert "conversation_id" not in kwargs


async def test_openai_agents_conversation_comes_from_the_hosts_mapping() -> None:
    runner = FakeRunner()
    conversations = {("tenant-a", "session-1"): "conv_a1"}
    adapter = OpenAIAgentsAdapter(
        object(),
        runner=runner,
        conversation_id=lambda request: conversations.get((request.tenant_id, request.session_id)),
    )

    await adapter.invoke(_request(tenant="tenant-a"))
    await adapter.invoke(_request(tenant="tenant-b"))

    assert runner.kwargs[0]["conversation_id"] == "conv_a1"
    assert "conversation_id" not in runner.kwargs[1]


@pytest.mark.parametrize(
    "option", ["context", "conversation_id", "previous_response_id", "run_config", "session"]
)
async def test_openai_agents_options_cannot_replace_identity_or_tracing(option: str) -> None:
    runner = FakeRunner()
    adapter = OpenAIAgentsAdapter(object(), runner=runner)

    result = await adapter.invoke(_request(openai_agents_options={option: "other"}))

    assert result.status == FrameworkRunStatus.FAILED
    assert runner.kwargs == []


async def test_openai_agents_other_options_and_the_constructors_run_config_pass_through() -> None:
    runner = FakeRunner()
    run_config = object()
    adapter = OpenAIAgentsAdapter(object(), runner=runner, run_config=run_config)

    await adapter.invoke(_request(openai_agents_options={"max_turns": 3}))

    assert runner.kwargs[0]["max_turns"] == 3
    assert runner.kwargs[0]["run_config"] is run_config


async def test_openai_agents_interruptions_carry_the_call_id_and_arguments() -> None:
    result = await OpenAIAgentsAdapter(object(), runner=FakeRunner(interrupt=True)).invoke(
        _request()
    )

    assert result.status == FrameworkRunStatus.AWAITING_INPUT
    assert result.metadata["interruptions"] == [
        {
            "type": "tool_approval_item",
            "tool_name": "issue_refund",
            "call_id": "call-9",
            "arguments": {"amount": 40},
        }
    ]
    assert result.metadata["interrupts"] == [
        {"id": "call-9", "value": {"tool_name": "issue_refund", "arguments": {"amount": 40}}}
    ]
    # The RunResult is kept, so the host can call to_state() and approve or reject.
    assert result.resume_state is not None


# --------------------------------------------------------------------------------------------
# AutoGen
# --------------------------------------------------------------------------------------------


async def test_autogen_instance_does_not_carry_one_requests_conversation_into_the_next() -> None:
    team = FakeTeam()
    adapter = AutoGenAdapter(team)

    first = await adapter.invoke(_request(tenant="tenant-a"))
    second = await adapter.invoke(_request(tenant="tenant-b"))

    assert first.output == "seen: refund order 42"
    assert second.output == "seen: refund order 42"
    assert team.resets == 2


async def test_autogen_instance_runs_one_request_at_a_time() -> None:
    team = FakeTeam()
    adapter = AutoGenAdapter(team)

    results = await asyncio.gather(
        adapter.invoke(_request(tenant="tenant-a", run="run-a")),
        adapter.invoke(_request(tenant="tenant-b", run="run-b")),
    )

    assert team.max_running == 1
    assert {result.output for result in results} == {"seen: refund order 42"}


async def test_autogen_factory_builds_a_team_per_request() -> None:
    built: list[tuple[str, FakeTeam]] = []

    def factory(request: FrameworkRunRequest) -> FakeTeam:
        team = FakeTeam()
        built.append((request.tenant_id, team))
        return team

    adapter = AutoGenAdapter(factory=factory)
    await adapter.invoke(_request(tenant="tenant-a"))
    await adapter.invoke(_request(tenant="tenant-b"))

    assert [tenant for tenant, _ in built] == ["tenant-a", "tenant-b"]
    assert built[0][1] is not built[1][1]


def test_autogen_takes_an_instance_or_a_factory() -> None:
    with pytest.raises(ValueError):
        AutoGenAdapter()
    with pytest.raises(ValueError):
        AutoGenAdapter(FakeTeam(), factory=lambda request: FakeTeam())


async def test_autogen_maps_a_handoff_to_the_user_to_awaiting_input() -> None:
    result = await AutoGenAdapter(FakeTeam(handoff_to="user")).invoke(_request())
    other = await AutoGenAdapter(FakeTeam(handoff_to="billing_agent")).invoke(_request())

    assert result.status == FrameworkRunStatus.AWAITING_INPUT
    assert result.metadata["interrupts"][0]["id"] == "handoff-1"
    assert result.metadata["interrupts"][0]["value"]["source"] == "refund_agent"
    assert other.status == FrameworkRunStatus.COMPLETED


async def test_autogen_capabilities_are_honest() -> None:
    capabilities = AutoGenAdapter(FakeTeam()).capabilities

    assert capabilities.persistence is False
    assert capabilities.human_in_the_loop is False


# --------------------------------------------------------------------------------------------
# CrewAI
# --------------------------------------------------------------------------------------------


async def test_crewai_cancellation_is_advertised_only_with_native_akickoff() -> None:
    assert CrewAIAdapter(FakeCrew()).capabilities.cancellation is True
    assert CrewAIAdapter(FakeCrew(asynchronous=False)).capabilities.cancellation is False
    assert CrewAIAdapter(FakeCrew()).capabilities.streaming is False

    # CrewAI's kickoff_async runs kickoff in a worker thread, which cancel() cannot stop.
    class ThreadedCrew(FakeCrew):
        async def kickoff_async(self, *, inputs: dict[str, Any]) -> Any:
            return await asyncio.to_thread(self.kickoff, inputs=inputs)

    threaded = CrewAIAdapter(ThreadedCrew(asynchronous=False))
    assert threaded.capabilities.cancellation is False
    assert (await threaded.invoke(_request())).output == "refund approved"


async def test_crewai_keeps_structured_output() -> None:
    result = await CrewAIAdapter(FakeCrew(asynchronous=False)).invoke(_request())

    assert result.output == "refund approved"
    assert result.metadata["value"] == {"refund": 40, "currency": "EUR"}
    assert result.usage.total_tokens == 6

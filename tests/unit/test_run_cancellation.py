"""Cancelling a run while its model call is in flight stops the provider's HTTP request.

Every way a run is stopped is covered: ``Runtime.cancel``, the HTTP cancel route, a conversation
turn's cancel, a cancelled workflow, the cancelled parent of a delegated child run, a run's
deadline, and service shutdown. The provider is a real ``OpenAICompatibleProvider`` talking to a
local fake API (``tests/fake_model_server.py``) that streams two chunks and stalls, or holds a
plain call open, and records when the client hangs up. Each test checks that

- the run ends ``cancelled`` (``timed_out`` for a deadline; recoverable on shutdown), with one
  ``run.cancelled`` event, last, and no model or tool call after it;
- the provider's response was closed and the server saw the connection go;
- the usage meter released the reservation of a call that delivered nothing and charged an
  estimate for a stream that had delivered text;
- no task is left behind.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from conftest import make_agent, make_runtime
from fake_model_server import FakeModelServer, TrackingTransport, no_tasks_left, tracked_client

from algen_agent_runtime import MeterCall
from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.conversations import (
    ConversationHandlerRegistry,
    ConversationService,
    MessageStatus,
    RuntimeConversationHandler,
)
from algen_agent_runtime.conversations.stores import (
    InMemoryConversationEventBus,
    InMemoryConversationStore,
)
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.models.providers.openai_compatible import OpenAICompatibleProvider
from algen_agent_runtime.orchestration.container import Container
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore, InMemoryRunStore
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.subagent import subagent_tool
from algen_agent_runtime.types.contracts import (
    ModelCapabilities,
    ModelProfile,
    RunRequest,
    RunState,
    RunStatus,
    TokenUsage,
)
from algen_agent_runtime.workflows import MultiAgentWorkflowExecutor, WorkflowHookRegistry
from algen_agent_runtime.workflows.contracts import (
    WorkflowManifest,
    WorkflowNode,
    WorkflowNodeKind,
)

HANG_UP_SECONDS = 2.0
# Long enough for a retry, a second step or a resumed drive to reach the server, had one started.
QUIET_SECONDS = 0.3
FAKE = ModelProfile(name="fake", provider="fake", model="fake-model")


class RecordingMeter:
    def __init__(self) -> None:
        self.reserved: list[MeterCall] = []
        self.settled: list[tuple[TokenUsage, bool]] = []
        self.released: list[object] = []

    async def reserve(
        self, *, max_input_tokens: int, max_output_tokens: int, call: MeterCall
    ) -> object:
        self.reserved.append(call)
        return len(self.reserved)

    async def settle(self, ticket: object, usage: TokenUsage, *, estimated: bool = False) -> None:
        self.settled.append((usage, estimated))

    async def release(self, ticket: object) -> None:
        self.released.append(ticket)


def _provider(server: FakeModelServer, client: httpx.AsyncClient) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        "fake",
        server.url,
        None,
        "fake-model",
        ModelCapabilities(streaming=True, tools=True),
        client=client,
    )


def _runtime(
    server: FakeModelServer,
    client: httpx.AsyncClient,
    *,
    meter: RecordingMeter | None = None,
    **agent: Any,
) -> AgentRuntime:
    runtime = make_runtime(_provider(server, client), make_agent(default_model=FAKE, **agent))
    router = ModelRouter(circuit_failure_threshold=1, usage_meter=meter)
    router.register_provider(_provider(server, client))
    runtime.router = router
    return runtime


def _request(*, stream: bool, **updates: Any) -> RunRequest:
    return RunRequest(
        agent="test-agent", input="hello", tenant_id="t", user_id="u", stream=stream, **updates
    )


async def _assert_hung_up(server: FakeModelServer, transport: TrackingTransport) -> None:
    await asyncio.wait_for(server.last.disconnected.wait(), HANG_UP_SECONDS)
    assert not server.last.finished
    assert transport.open_responses == 0


async def _assert_quiet(server: FakeModelServer, requests: int = 1) -> None:
    """No model call (a retry, a next step, a resumed drive) reaches the server afterwards."""
    await asyncio.sleep(QUIET_SECONDS)
    assert len(server.exchanges) == requests


async def _assert_cancelled(runtime: AgentRuntime, run_id: str) -> None:
    state = await runtime.status(run_id, "t")
    assert state.status == RunStatus.CANCELLED
    history = [event.type for event in await runtime.events.history(run_id)]
    assert history.count("run.cancelled") == 1
    assert history[-1] == "run.cancelled"
    assert "model.completed" not in history
    assert not any(kind.startswith("tool.") for kind in history)
    # The cancelled call to the fake provider never completed, so it was never audited.
    assert not await runtime.audits.list("t", "fake/fake-model")


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "plain"])
async def test_cancelling_a_run_stops_its_provider_request(stream: bool) -> None:
    client, transport = tracked_client()
    meter = RecordingMeter()
    # Enough text for the stream guard (which holds back a tail) to record some of it.
    async with FakeModelServer(chunks=40, delay=None) as server:
        runtime = _runtime(server, client, meter=meter)
        state = await runtime.start(_request(stream=stream))
        await server.wait_for_request()
        if stream:
            await _streamed_text(runtime, state.id)

        cancelled = await runtime.cancel(state.id, "t")
        result = await runtime.wait(state.id, "t")

        assert cancelled.status == RunStatus.CANCELLED
        assert result.status == RunStatus.CANCELLED
        await _assert_hung_up(server, transport)
        await _assert_quiet(server)
        await _assert_cancelled(runtime, state.id)
        assert len(meter.reserved) == 1
        if stream:
            # Text reached the run: the provider bills it, so an estimate is charged.
            [(usage, estimated)] = meter.settled
            assert estimated
            assert usage.output_tokens > 0
            assert meter.released == []
        else:
            # Nothing came back: the reservation is released, nothing is charged.
            assert meter.settled == []
            assert meter.released == [1]
        await runtime.shutdown(grace_seconds=1)
    await client.aclose()
    await no_tasks_left()


async def _streamed_text(runtime: AgentRuntime, run_id: str) -> None:
    """Wait until the run has received streamed text (its first ``model.delta``)."""
    for _ in range(400):
        if any(e.type == "model.delta" for e in await runtime.events.history(run_id)):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the run never streamed text")


async def test_cancelling_over_http_stops_the_provider_request() -> None:
    client, transport = tracked_client()
    async with FakeModelServer() as server:
        runtime = _runtime(server, client)
        container = Container(
            runtime=runtime,
            agents=runtime.agents,
            tools=runtime.tools,
            artifacts=InMemoryArtifactStore(),
            events=runtime.events,
            conversations=ConversationService(
                InMemoryConversationStore(),
                InMemoryConversationEventBus(),
                ConversationHandlerRegistry(),
            ),
        )
        headers = {"x-tenant-id": "t", "x-user-id": "u", "x-scopes": "runs:read runs:write"}
        app = httpx.ASGITransport(app=create_app(AppSettings(), container))
        async with httpx.AsyncClient(transport=app, base_url="http://test") as api:
            started = await api.post(
                "/v1/runs",
                headers=headers,
                json={"agent": "test-agent", "input": "hello", "stream": True},
            )
            assert started.status_code == 202, started.text
            run_id = started.json()["run_id"]
            await server.wait_for_request()

            cancelled = await api.delete(f"/v1/runs/{run_id}", headers=headers)

            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["status"] == RunStatus.CANCELLED
            await runtime.wait(run_id, "t")
            await _assert_hung_up(server, transport)
            await _assert_quiet(server)
            await _assert_cancelled(runtime, run_id)
        await runtime.shutdown(grace_seconds=1)
    await client.aclose()


async def test_a_run_past_its_deadline_stops_its_provider_request() -> None:
    client, transport = tracked_client()
    async with FakeModelServer() as server:
        runtime = _runtime(server, client)

        result = await runtime.run(_request(stream=True, timeout_seconds=0.5))

        assert result.status == RunStatus.TIMED_OUT
        await _assert_hung_up(server, transport)
        await _assert_quiet(server)
        history = [event.type for event in await runtime.events.history(result.run_id)]
        assert history.count("run.failed") == 1
        assert "run.cancelled" not in history
        await runtime.shutdown(grace_seconds=1)
    await client.aclose()
    await no_tasks_left()


async def test_shutdown_stops_the_provider_request_and_keeps_the_run_recoverable() -> None:
    client, transport = tracked_client()
    async with FakeModelServer() as server:
        runtime = _runtime(server, client)
        state = await runtime.start(_request(stream=True))
        await server.wait_for_request()

        await runtime.shutdown(grace_seconds=0.05)

        await _assert_hung_up(server, transport)
        await _assert_quiet(server)
        stopped = await runtime.status(state.id, "t")
        # Not cancelled: the next start's recover() drives it again.
        assert stopped.status not in {RunStatus.CANCELLED, RunStatus.FAILED}
        history = [event.type for event in await runtime.events.history(state.id)]
        assert "run.cancelled" not in history
    await client.aclose()
    await no_tasks_left()


async def test_cancelling_a_conversation_turn_stops_its_provider_request() -> None:
    client, transport = tracked_client()
    async with FakeModelServer() as server:
        runtime = _runtime(server, client)
        handlers = ConversationHandlerRegistry()
        handlers.register(RuntimeConversationHandler(runtime))
        service = ConversationService(
            InMemoryConversationStore(), InMemoryConversationEventBus(), handlers
        )
        conversation = await service.create(tenant_id="t", user_id="u", agent="test-agent")
        await service.submit(conversation.id, "t", "u", "hello")
        exchange = await server.wait_for_request()
        assert exchange.body["stream"] is True  # a turn streams when every candidate can

        await service.cancel(conversation.id, "t")

        await _assert_hung_up(server, transport)
        message = await _last_message(service, conversation.id, MessageStatus.CANCELLED)
        [run_id] = message.run_ids
        await runtime.wait(run_id, "t")
        await _assert_quiet(server)
        await _assert_cancelled(runtime, run_id)
        await runtime.shutdown(grace_seconds=1)
    await client.aclose()


async def _last_message(service: ConversationService, conversation_id: str, status: Any) -> Any:
    for _ in range(400):
        messages = await service.messages(conversation_id, "t")
        if messages and messages[-1].status == status:
            return messages[-1]
        await asyncio.sleep(0.005)
    raise AssertionError(f"the turn never became {status}")


async def test_cancelling_a_workflow_stops_its_agent_steps_provider_request() -> None:
    client, transport = tracked_client()
    async with FakeModelServer() as server:
        runtime = _runtime(server, client)
        manifest = WorkflowManifest(
            name="one-step",
            version="1.0.0",
            nodes=(
                WorkflowNode(
                    id="answer",
                    kind=WorkflowNodeKind.AGENT,
                    agent="test-agent",
                    input_template="{{input}}",
                    output_key="a",
                ),
            ),
        )
        executor = MultiAgentWorkflowExecutor(runtime, WorkflowHookRegistry())
        workflow = asyncio.create_task(
            executor.run(manifest, {"input": "hello"}, tenant_id="t", user_id="u")
        )
        await server.wait_for_request(first_chunk=False)

        workflow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await workflow

        await _assert_hung_up(server, transport)
        await _assert_quiet(server)
        runs = await _runs(runtime)
        assert [run.status for run in runs] == [RunStatus.CANCELLED]
        await _assert_cancelled(runtime, runs[0].id)
        await runtime.shutdown(grace_seconds=1)
    await client.aclose()


async def _runs(runtime: AgentRuntime) -> list[RunState]:
    store = runtime.runs
    assert isinstance(store, InMemoryRunStore)
    return [state.model_copy(deep=True) for state in store._runs.values()]


async def test_cancelling_a_parent_run_stops_its_delegated_childs_provider_request() -> None:
    client, transport = tracked_client()
    async with FakeModelServer() as server:
        parent_model = MockModelProvider(
            [tool_call_response("core.subagent", {"agent": "child-agent", "input": "look"})]
        )
        runtime = make_runtime(
            parent_model,
            make_agent(
                enabled_tools=frozenset({"core.subagent"}),
                tool_permissions=frozenset({"agents.delegate"}),
                delegates=frozenset({"child-agent"}),
            ),
        )
        runtime.router.register_provider(_provider(server, client))
        runtime.agents.register(make_agent(name="child-agent", default_model=FAKE))
        runtime.tools.register(subagent_tool(runtime))
        parent = await runtime.start(_request(stream=True))
        await server.wait_for_request()

        await runtime.cancel(parent.id, "t")
        await runtime.wait(parent.id, "t")

        await _assert_hung_up(server, transport)
        await _assert_quiet(server)
        runs = {run.request.agent: run for run in await _runs(runtime)}
        assert runs["test-agent"].status == RunStatus.CANCELLED
        assert runs["child-agent"].status == RunStatus.CANCELLED
        await _assert_cancelled(runtime, runs["child-agent"].id)
        # The parent stopped at the cancel: its core.subagent call did not go on to complete
        # (and be recorded) with the child's cancelled result.
        parent_events = [event.type for event in await runtime.events.history(parent.id)]
        assert parent_events.count("run.cancelled") == 1
        assert parent_events[-1] == "run.cancelled", parent_events
        assert "tool.completed" not in parent_events
        assert len(parent_model.requests) == 1  # the parent's model was not asked again
        await runtime.shutdown(grace_seconds=1)
    await client.aclose()


class SlowSaveRunStore(InMemoryRunStore):
    """A run store whose writes take a moment, as a database's do."""

    async def save(self, state: RunState, expected_version: int) -> None:
        await asyncio.sleep(0.01)
        await super().save(state, expected_version)


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "plain"])
async def test_cancel_and_the_cancelled_drive_do_not_race_on_a_slow_store(stream: bool) -> None:
    # ``cancel`` marks the run cancelled while the drive it cancelled does the same as it
    # unwinds. On a store whose writes yield, both used to write from the same version: one of
    # them failed with a conflict, either ``cancel`` itself or the drive (its task then ended
    # with RunConflictError instead of cancelled).
    client, transport = tracked_client()
    async with FakeModelServer() as server:
        runtime = _runtime(server, client)
        runtime.runs = SlowSaveRunStore()
        state = await runtime.start(_request(stream=stream))
        await server.wait_for_request()
        task = runtime._tasks[state.id]

        cancelled = await runtime.cancel(state.id, "t")
        result = await runtime.wait(state.id, "t")

        assert cancelled.status == RunStatus.CANCELLED
        assert result.status == RunStatus.CANCELLED
        assert task.cancelled() or task.exception() is None
        await _assert_hung_up(server, transport)
        await _assert_cancelled(runtime, state.id)
        await runtime.shutdown(grace_seconds=1)
    await client.aclose()


async def test_cancelling_a_run_twice_is_harmless() -> None:
    client, _ = tracked_client()
    async with FakeModelServer() as server:
        runtime = _runtime(server, client)
        state = await runtime.start(_request(stream=True))
        await server.wait_for_request()

        first, second = await asyncio.gather(
            runtime.cancel(state.id, "t"), runtime.cancel(state.id, "t")
        )

        assert first.status == second.status == RunStatus.CANCELLED
        assert (await runtime.wait(state.id, "t")).status == RunStatus.CANCELLED
        await _assert_cancelled(runtime, state.id)
        await runtime.shutdown(grace_seconds=1)
    await client.aclose()


async def test_a_run_that_is_not_cancelled_completes_against_the_same_server() -> None:
    # The same set-up, left alone, completes: the tests above fail for the cancellation only.
    client, transport = tracked_client()
    meter = RecordingMeter()
    async with FakeModelServer(finish=True, delay=0) as server:
        runtime = _runtime(server, client, meter=meter)
        streamed = await runtime.run(_request(stream=True))
        plain = await runtime.run(_request(stream=False))
        assert streamed.status == plain.status == RunStatus.COMPLETED
        assert all(exchange.finished for exchange in server.exchanges)
        assert transport.open_responses == 0
        assert await runtime.audits.list("t", "fake/fake-model")
        assert [estimated for _, estimated in meter.settled] == [False, False]
        await runtime.shutdown(grace_seconds=1)
    await client.aclose()


async def test_a_caller_cancelled_while_it_waits_for_a_run_is_cancelled_too() -> None:
    # Cancelling a task that awaits ``Runtime.run`` cancels the run's drive, which ends the run
    # cancelled and returns normally. The waiter used to return that result as if it had not
    # been cancelled (here: no TimeoutError); it now raises CancelledError, as asyncio expects.
    client, transport = tracked_client()
    async with FakeModelServer() as server:
        runtime = _runtime(server, client)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.5):
                await runtime.run(_request(stream=True))

        [run] = await _runs(runtime)
        assert run.status == RunStatus.CANCELLED
        await _assert_hung_up(server, transport)
        await _assert_cancelled(runtime, run.id)
        await runtime.shutdown(grace_seconds=1)
    await client.aclose()
    await no_tasks_left()

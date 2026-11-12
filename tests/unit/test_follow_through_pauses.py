"""Following a run through its pauses (A2), and subscribers that fall behind lose nothing
silently."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.api import app as app_module
from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.conversations.service import (
    ConversationHandlerRegistry,
    ConversationService,
    RuntimeConversationHandler,
)
from algen_agent_runtime.conversations.stores import (
    InMemoryConversationEventBus,
    InMemoryConversationStore,
)
from algen_agent_runtime.events.bus import InMemoryEventBus
from algen_agent_runtime.events.contracts import RunEvent
from algen_agent_runtime.exceptions.errors import EventStreamOverflowError
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import Container
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)
from algen_agent_runtime.types.contracts import RunRequest, RunStatus

HEADERS = {"x-tenant-id": "tenant", "x-user-id": "user", "x-scopes": "runs:read runs:write"}


def _request() -> RunRequest:
    return RunRequest(agent="test-agent", input="write it", tenant_id="tenant", user_id="user")


def _runtime(*responses: Any) -> AgentRuntime:
    provider = MockModelProvider(
        list(responses) or [tool_call_response("api.write", {"value": "x"}), "saved"]
    )
    runtime = make_runtime(
        provider,
        make_agent(
            enabled_tools=frozenset({"api.write", "core.ask_user"}),
            tool_permissions=frozenset({"user.ask"}),
        ),
    )

    def write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        return {"ok": True}

    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="api.write",
                version="1",
                description="Write a record.",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                side_effect=SideEffect.WRITE,
                idempotency=Idempotency.KEYED,
            ),
            write,
        )
    )
    return runtime


async def _paused(runtime: AgentRuntime, run_id: str) -> None:
    for _ in range(400):
        if (await runtime.status(run_id, "tenant")).status in {
            RunStatus.AWAITING_APPROVAL,
            RunStatus.AWAITING_CLARIFICATION,
        }:
            await runtime.wait(run_id, "tenant")
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the run never paused")


def _http(runtime: AgentRuntime) -> httpx.AsyncClient:
    handlers = ConversationHandlerRegistry()
    handlers.register(RuntimeConversationHandler(runtime))
    container = Container(
        runtime=runtime,
        agents=runtime.agents,
        tools=runtime.tools,
        artifacts=InMemoryArtifactStore(),
        events=runtime.events,
        conversations=ConversationService(
            InMemoryConversationStore(), InMemoryConversationEventBus(), handlers
        ),
    )
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _types(text: str) -> list[str]:
    return [
        json.loads(line.removeprefix("data: "))["type"]
        for line in text.splitlines()
        if line.startswith("data: ") and '"type"' in line
    ]


# The client ---------------------------------------------------------------------------------


async def test_follow_yields_one_stream_through_an_approval_until_the_run_ends() -> None:
    runtime = _runtime()
    client = AlgenAgentRuntimeClient(runtime)
    state = await runtime.start(_request())
    seen: list[RunEvent] = []

    async def follow() -> None:
        async for event in client.follow(state.id):
            seen.append(event)

    task = asyncio.create_task(follow())
    await _paused(runtime, state.id)
    await asyncio.sleep(0.01)
    assert not task.done()
    assert seen[-1].type == "approval.required"

    await runtime.resume(state.id, "tenant", {"decision": "approved"})
    await asyncio.wait_for(task, 5)

    types = [event.type for event in seen]
    assert types[-1] == "run.completed"
    assert types.index("approval.required") < types.index("tool.completed")
    assert [event.sequence for event in seen] == list(range(1, len(seen) + 1))
    await runtime.shutdown(grace_seconds=5)


async def test_stream_through_pauses_spans_several_pauses() -> None:
    runtime = _runtime(
        tool_call_response("core.ask_user", {"question": "Which record?"}),
        tool_call_response("api.write", {"value": "x"}),
        "saved",
    )
    client = AlgenAgentRuntimeClient(runtime)
    seen: list[str] = []
    answering: set[asyncio.Task[None]] = set()

    async def stream() -> None:
        async for event in client.stream(_request(), through_pauses=True):
            seen.append(event.type)
            if event.type in {"clarification.required", "approval.required"}:
                # Answered by someone else while this one subscription stays open.
                answering.add(asyncio.create_task(answer(event.run_id, event.type)))

    async def answer(run_id: str, kind: str) -> None:
        await runtime.wait(run_id, "tenant")
        payload = (
            {"clarification": "the first"}
            if kind == "clarification.required"
            else {"decision": "approved"}
        )
        await runtime.resume(run_id, "tenant", payload)

    await asyncio.wait_for(stream(), 5)

    assert seen[-1] == "run.completed"
    assert seen.count("clarification.required") == 1 and seen.count("approval.required") == 1
    await runtime.shutdown(grace_seconds=5)


async def test_stream_events_still_ends_at_a_pause_by_default() -> None:
    runtime = _runtime()
    client = AlgenAgentRuntimeClient(runtime)

    events = [event async for event in client.stream(_request())]

    assert events[-1].type == "approval.required"
    await runtime.shutdown(grace_seconds=5)


async def test_an_expired_pause_ends_the_followed_run() -> None:
    runtime = _runtime(tool_call_response("core.ask_user", {"question": "Which record?"}))
    client = AlgenAgentRuntimeClient(runtime)
    state = await runtime.start(_request())
    await _paused(runtime, state.id)

    assert await runtime.expire_paused_runs(clarification_timeout_seconds=0) == 1
    events = [event async for event in client.follow(state.id)]

    assert events[-1].type == "run.failed"
    assert events[-1].data["reason"] == "clarification_expired"


# SSE ----------------------------------------------------------------------------------------


async def test_the_sse_stream_follows_through_pauses_with_keepalives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "_SSE_KEEPALIVE_SECONDS", 0.01)
    runtime = _runtime()
    state = await runtime.start(_request())
    async with _http(runtime) as client:
        request = asyncio.create_task(
            client.get(f"/v1/runs/{state.id}/events?through_pauses=true", headers=HEADERS)
        )
        await _paused(runtime, state.id)
        await asyncio.sleep(0.05)  # a few keepalive intervals while the run waits
        assert not request.done()
        await runtime.resume(state.id, "tenant", {"decision": "approved"})
        response = await asyncio.wait_for(request, 5)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    types = _types(response.text)
    assert "approval.required" in types and types[-1] == "run.completed"
    assert ": keepalive" in response.text
    await runtime.shutdown(grace_seconds=5)


async def test_the_default_sse_stream_sends_no_keepalives_and_ends_at_the_pause() -> None:
    runtime = _runtime()
    state = await runtime.start(_request())
    async with _http(runtime) as client:
        response = await asyncio.wait_for(
            client.get(f"/v1/runs/{state.id}/events", headers=HEADERS), timeout=5
        )

    assert _types(response.text)[-1] == "approval.required"
    assert "keepalive" not in response.text
    await runtime.shutdown(grace_seconds=5)


async def test_an_overflowing_sse_subscriber_is_told_where_to_reconnect() -> None:
    runtime = _runtime()
    state = await runtime.start(_request())
    await _paused(runtime, state.id)
    durable = runtime.events

    class OverflowingBus(InMemoryEventBus):
        async def subscribe(self, run_id: str, after: int = 0) -> Any:  # type: ignore[override]
            async for event in durable.subscribe(run_id, after):
                yield event
                if event.sequence == after + 2:
                    raise EventStreamOverflowError(run_id, event.sequence)

    runtime.events = OverflowingBus()
    async with _http(runtime) as client:
        response = await asyncio.wait_for(
            client.get(f"/v1/runs/{state.id}/events", headers=HEADERS), timeout=5
        )

    frames = response.text.strip().split("\n\n")
    assert frames[-1].startswith("id: 2\nevent: stream.overflow\n")
    assert json.loads(frames[-1].split("data: ")[1].split("\n")[0]) == {
        "last_sequence": 2,
        "reconnect": True,
    }


# The event bus ------------------------------------------------------------------------------


def _event(run_id: str, sequence: int, kind: str = "model.delta") -> RunEvent:
    return RunEvent(
        type=kind,  # type: ignore[arg-type]
        run_id=run_id,
        tenant_id="t",
        session_id="s",
        correlation_id="c",
        sequence=sequence,
        data={},
    )


async def test_a_slow_subscriber_reads_on_from_the_history_and_misses_nothing() -> None:
    bus = InMemoryEventBus(subscriber_queue_size=3)
    iterator = bus.subscribe("run-1").__aiter__()
    await bus.publish(_event("run-1", 1))
    first = await iterator.__anext__()
    for sequence in range(2, 12):
        await bus.publish(
            _event("run-1", sequence, "run.completed" if sequence == 11 else "model.delta")
        )

    rest = [await iterator.__anext__() for _ in range(10)]

    assert [first.sequence, *(event.sequence for event in rest)] == list(range(1, 12))
    assert rest[-1].type == "run.completed"
    await iterator.aclose()  # type: ignore[attr-defined]


async def test_a_subscriber_behind_the_kept_history_gets_an_overflow_error() -> None:
    bus = InMemoryEventBus(history_limit=4, subscriber_queue_size=2)
    iterator = bus.subscribe("run-1").__aiter__()
    await bus.publish(_event("run-1", 1))
    assert (await iterator.__anext__()).sequence == 1
    for sequence in range(2, 12):
        await bus.publish(_event("run-1", sequence))

    delivered = [await iterator.__anext__() for _ in range(2)]
    with pytest.raises(EventStreamOverflowError) as raised:
        await iterator.__anext__()

    assert [event.sequence for event in delivered] == [2, 3]
    assert raised.value.last_sequence == 3
    assert raised.value.run_id == "run-1"

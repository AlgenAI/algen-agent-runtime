"""Steering a running run (``AgentRuntime.steer``) and tool notes (``ToolContext.emit_note``).

Steering messages reach the model at the next step boundary, after the current tool results and
before the next model call; those still waiting when the run ends come back on its result. A
tool note is a ``tool.note`` run event for the UI and never reaches the model.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.conversations.service import (
    ConversationHandlerRegistry,
    ConversationService,
)
from algen_agent_runtime.conversations.stores import (
    InMemoryConversationEventBus,
    InMemoryConversationStore,
)
from algen_agent_runtime.exceptions.errors import ConflictError, NotFoundError
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import Container
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import (
    MAX_TOOL_NOTE_BYTES,
    MAX_TOOL_NOTES_PER_CALL,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolNote,
)
from algen_agent_runtime.types.contracts import (
    MAX_PENDING_STEERING,
    MAX_STEERING_CHARS,
    ModelRequest,
    ModelResponse,
    Role,
    RunRequest,
    RunState,
    RunStatus,
    SteeringMessage,
)


class Gate:
    """A tool handler that waits until it is released, so a test can act mid-call."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def handler(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        self.entered.set()
        await self.release.wait()
        return {"ok": True}


class GatedProvider(MockModelProvider):
    """A mock provider whose replies from ``gate_from`` on wait until released."""

    def __init__(self, responses: list[str | ModelResponse], gate_from: int) -> None:
        super().__init__(responses)
        self.gate_from = gate_from
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if len(self.requests) >= self.gate_from:
            self.entered.set()
            await self.release.wait()
        return await super().generate(request)


def _tool(name: str, handler: Any, side_effect: SideEffect = SideEffect.READ) -> Tool:
    return Tool(
        ToolDefinition(
            name=name,
            version="1",
            description=name,
            side_effect=side_effect,
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        ),
        handler,
    )


def _runtime(provider: MockModelProvider, *tools: Tool, **agent: Any) -> AgentRuntime:
    runtime = make_runtime(
        provider,
        make_agent(enabled_tools=frozenset(tool.definition.name for tool in tools), **agent),
    )
    for tool in tools:
        runtime.tools.register(tool)
    return runtime


def _request() -> RunRequest:
    return RunRequest(agent="test-agent", input="go", tenant_id="t", user_id="u")


async def _types(runtime: AgentRuntime, run_id: str) -> list[str]:
    return [event.type for event in await runtime.events.history(run_id)]


# Steering


async def test_a_steering_message_reaches_the_model_after_the_tool_results() -> None:
    gate = Gate()
    provider = MockModelProvider([tool_call_response("files.read", {}), "answered"])
    runtime = _runtime(provider, _tool("files.read", gate.handler))
    state = await runtime.start(_request())
    await gate.entered.wait()
    queued = await runtime.steer(state.id, "t", "  also check the README  ")
    assert queued.text == "also check the README"
    gate.release.set()
    result = await runtime.wait(state.id, "t")
    assert result.status == RunStatus.COMPLETED and result.undelivered_steering == ()
    # The second model call sees the tool result, then the user's message.
    tail = provider.requests[1].messages[-2:]
    assert [message.role for message in tail] == [Role.TOOL, Role.USER]
    assert tail[1].text_content == "also check the README"
    events = await runtime.events.history(state.id)
    types = [event.type for event in events]
    assert types.index("tool.completed") < types.index("steer.delivered")
    assert types.index("steer.delivered") < len(types) - 1 - types[::-1].index("model.started")
    [delivered] = [event for event in events if event.type == "steer.delivered"]
    # Without content telemetry, events carry ids and sizes, never the text.
    assert delivered.data == {
        "count": 1,
        "messages": [{"id": queued.id, "chars": len("also check the README")}],
    }
    assert (await runtime.status(state.id, "t")).pending_steering == []
    audits = await runtime.audits.list("t", state.id)
    assert ("run.steer", "delivered") in [(audit.action, audit.outcome) for audit in audits]


async def test_several_messages_arrive_together_in_one_user_message() -> None:
    gate = Gate()
    provider = MockModelProvider([tool_call_response("files.read", {}), "answered"])
    runtime = _runtime(provider, _tool("files.read", gate.handler))
    state = await runtime.start(_request())
    await gate.entered.wait()
    await runtime.steer(state.id, "t", "first")
    await runtime.steer(state.id, "t", "second")
    gate.release.set()
    await runtime.wait(state.id, "t")
    last = provider.requests[1].messages[-1]
    assert last.role == Role.USER
    assert [block.text for block in last.content] == ["first", "second"]  # type: ignore[union-attr]


async def test_messages_not_delivered_before_the_run_ends_come_back_on_its_result() -> None:
    provider = GatedProvider(["the answer"], gate_from=0)
    runtime = _runtime(provider)
    runtime.telemetry_include_content = True
    state = await runtime.start(_request())
    await provider.entered.wait()
    queued = await runtime.steer(state.id, "t", "too late")
    provider.release.set()
    result = await runtime.wait(state.id, "t")
    assert result.status == RunStatus.COMPLETED and result.output == "the answer"
    assert [message.text for message in result.undelivered_steering] == ["too late"]
    assert result.undelivered_steering[0].id == queued.id
    events = await runtime.events.history(state.id)
    [undelivered] = [event for event in events if event.type == "steer.undelivered"]
    # With content telemetry on, the text is in the event too, so a UI can hand it back.
    assert undelivered.data["messages"] == [{"id": queued.id, "chars": 8, "text": "too late"}]
    assert "steer.delivered" not in [event.type for event in events]
    # The checkpoint keeps them, and steering an ended run is refused.
    assert [item.text for item in (await runtime.status(state.id, "t")).pending_steering] == [
        "too late"
    ]
    with pytest.raises(ConflictError, match="has ended"):
        await runtime.steer(state.id, "t", "again")


async def test_a_paused_run_keeps_its_steering_until_it_is_resumed() -> None:
    gate = Gate()
    gate.release.set()
    provider = MockModelProvider([tool_call_response("config.change", {}), "changed"])
    runtime = _runtime(provider, _tool("config.change", gate.handler, SideEffect.WRITE))
    paused = await runtime.run(_request())
    assert paused.status == RunStatus.AWAITING_APPROVAL
    queued = await runtime.steer(paused.run_id, "t", "use the staging config")
    # Saved in the checkpoint, so a restart while paused keeps it.
    assert (await runtime.status(paused.run_id, "t")).pending_steering == [queued]
    await runtime.resume(paused.run_id, "t", {"decision": "approved"})
    result = await runtime.wait(paused.run_id, "t")
    assert result.status == RunStatus.COMPLETED and result.undelivered_steering == ()
    assert provider.requests[-1].messages[-1].text_content == "use the staging config"


async def test_cancelling_a_paused_run_returns_its_steering_as_undelivered() -> None:
    provider = MockModelProvider([tool_call_response("config.change", {}), "changed"])
    runtime = _runtime(
        provider, _tool("config.change", lambda arguments, context: {}, SideEffect.WRITE)
    )
    paused = await runtime.run(_request())
    await runtime.steer(paused.run_id, "t", "never mind")
    await runtime.cancel(paused.run_id, "t")
    result = await runtime.wait(paused.run_id, "t")
    assert result.status == RunStatus.CANCELLED
    assert [message.text for message in result.undelivered_steering] == ["never mind"]
    assert "steer.undelivered" in await _types(runtime, paused.run_id)


async def test_steering_is_scoped_to_the_runs_tenant() -> None:
    gate = Gate()
    runtime = _runtime(
        MockModelProvider([tool_call_response("files.read", {}), "done"]),
        _tool("files.read", gate.handler),
    )
    state = await runtime.start(_request())
    await gate.entered.wait()
    with pytest.raises(NotFoundError):
        await runtime.steer(state.id, "other-tenant", "hello")
    gate.release.set()
    result = await runtime.wait(state.id, "t")
    assert "steer.delivered" not in await _types(runtime, result.run_id)


async def test_steering_text_and_queue_are_bounded() -> None:
    gate = Gate()
    runtime = _runtime(
        MockModelProvider([tool_call_response("files.read", {}), "done"]),
        _tool("files.read", gate.handler),
    )
    state = await runtime.start(_request())
    await gate.entered.wait()
    with pytest.raises(ValueError, match="required"):
        await runtime.steer(state.id, "t", "   ")
    with pytest.raises(ValueError, match=str(MAX_STEERING_CHARS)):
        await runtime.steer(state.id, "t", "x" * (MAX_STEERING_CHARS + 1))
    for index in range(MAX_PENDING_STEERING):
        await runtime.steer(state.id, "t", f"message {index}")
    with pytest.raises(ConflictError, match="already has"):
        await runtime.steer(state.id, "t", "one too many")
    gate.release.set()
    result = await runtime.wait(state.id, "t")
    assert result.status == RunStatus.COMPLETED
    delivered = [
        event for event in await runtime.events.history(state.id) if event.type == "steer.delivered"
    ]
    assert [event.data["count"] for event in delivered] == [MAX_PENDING_STEERING]


async def test_steering_waits_while_calls_from_the_same_reply_are_pending() -> None:
    gate = Gate()
    calls = ModelResponse.model_validate(
        {
            **tool_call_response("files.read", {}).model_dump(),
            "tool_calls": [
                {"id": "first", "name": "files.read", "arguments": {}},
                {"id": "second", "name": "files.list", "arguments": {}},
            ],
        }
    )
    provider = MockModelProvider([calls, "done"])
    runtime = _runtime(
        provider, _tool("files.read", gate.handler), _tool("files.list", lambda a, c: {})
    )
    state = await runtime.start(_request())
    await gate.entered.wait()
    await runtime.steer(state.id, "t", "between")
    gate.release.set()
    await runtime.wait(state.id, "t")
    # Both results stay next to the reply that asked for them; the message comes after them.
    roles = [message.role for message in provider.requests[1].messages[-4:]]
    assert roles == [Role.ASSISTANT, Role.TOOL, Role.TOOL, Role.USER]


async def test_a_checkpoint_saved_without_steering_still_loads() -> None:
    state = RunState(request=_request())
    data = json.loads(state.model_dump_json())
    # Empty, it is left out of the checkpoint: 0.1.0a17 forbids unknown fields.
    assert "pending_steering" not in data
    assert RunState.model_validate(data).pending_steering == []
    state.pending_steering.append(SteeringMessage(text="hi"))
    assert json.loads(state.model_dump_json())["pending_steering"][0]["text"] == "hi"
    assert SteeringMessage(text="hi").id


async def test_the_steer_route_queues_a_message_with_run_write_scope() -> None:
    gate = Gate()
    runtime = _runtime(
        MockModelProvider([tool_call_response("files.read", {}), "done"]),
        _tool("files.read", gate.handler),
    )
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
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    headers = {"x-tenant-id": "t", "x-user-id": "u", "x-scopes": "runs:read runs:write"}
    state = await runtime.start(_request())
    await gate.entered.wait()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        url = f"/v1/runs/{state.id}/steer"
        read_only = await client.post(
            url, headers={**headers, "x-scopes": "runs:read"}, json={"text": "hi"}
        )
        assert read_only.status_code == 403
        other = await client.post(url, headers={**headers, "x-tenant-id": "x"}, json={"text": "hi"})
        assert other.status_code == 404
        empty = await client.post(url, headers=headers, json={"text": ""})
        assert empty.status_code == 422
        blank = await client.post(url, headers=headers, json={"text": "   "})
        assert blank.status_code == 422
        queued = await client.post(url, headers=headers, json={"text": "look at main.py"})
        assert queued.status_code == 202
        body = queued.json()
        assert body["run_id"] == state.id and body["status"] == "queued" and body["steer_id"]
        gate.release.set()
        await runtime.wait(state.id, "t")
        ended = await client.post(url, headers=headers, json={"text": "late"})
        assert ended.status_code == 409


# Tool notes


async def test_a_tool_note_is_an_event_for_its_call_and_never_reaches_the_model() -> None:
    async def write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        note = await context.emit_note(
            title="Updated the config",
            paths=["config/app.yaml"],
            suggestions=["Run the checks"],
            data={"before_sha256": "a" * 64, "lines": 3},
        )
        assert isinstance(note, ToolNote)
        return {"ok": True}

    provider = MockModelProvider([tool_call_response("config.change", {}), "done"])
    runtime = _runtime(
        provider,
        _tool("config.change", write, SideEffect.WRITE),
        approval_policy={"require_for_side_effects": False, "require_for_destructive": False},
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED
    events = await runtime.events.history(result.run_id)
    [note] = [event for event in events if event.type == "tool.note"]
    [started] = [event for event in events if event.type == "tool.started"]
    assert note.step_id == started.step_id and note.step_id is not None
    assert note.data == {
        "tool": "config.change",
        "call_id": note.step_id.removeprefix("tool-"),
        "title": "Updated the config",
        "paths": ["config/app.yaml"],
        "suggestions": ["Run the checks"],
        "data": {"before_sha256": "a" * 64, "lines": 3},
    }
    types = [event.type for event in events]
    assert types.index("tool.started") < types.index("tool.note") < types.index("tool.completed")
    sent = json.dumps(
        [message.model_dump(mode="json") for message in provider.requests[-1].messages]
    )
    assert "Updated the config" not in sent and "config/app.yaml" not in sent
    narrative = await runtime.narrative(result.run_id, "t")
    [entry] = [entry for entry in narrative.entries if "note" in entry.summary]
    assert "Updated the config" not in json.dumps(entry.model_dump(mode="json"))


async def test_a_tool_note_past_its_bounds_is_refused() -> None:
    context = ToolContext(
        run_id="r",
        step_id="s",
        tenant_id="t",
        user_id="u",
        permissions=frozenset(),
        idempotency_key="k",
    )
    # Outside a run a note is checked and returned, and nothing is emitted.
    assert (await context.emit_note(title="ok")).title == "ok"
    with pytest.raises(ValueError):
        await context.emit_note(title="")
    with pytest.raises(ValueError):
        await context.emit_note(title="x" * 201)
    with pytest.raises(ValueError, match="sequences of strings"):
        await context.emit_note(title="t", paths="one/path")
    with pytest.raises(ValueError):
        await context.emit_note(title="t", paths=["p"] * 51)
    with pytest.raises(ValueError, match=str(MAX_TOOL_NOTE_BYTES)):
        await context.emit_note(title="t", data={"blob": "x" * MAX_TOOL_NOTE_BYTES})
    with pytest.raises(ValueError, match="JSON"):
        await context.emit_note(title="t", data={"value": object()})
    # The sink is private: never serialized or shown.
    assert "note_sink" not in context.model_dump() and "note_sink" not in repr(context)


async def test_a_call_emits_a_bounded_number_of_notes() -> None:
    raised: list[str] = []

    async def chatty(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        for index in range(MAX_TOOL_NOTES_PER_CALL + 1):
            try:
                await context.emit_note(title=f"note {index}")
            except ValueError as exc:
                raised.append(str(exc))
        return {"ok": True}

    runtime = _runtime(
        MockModelProvider([tool_call_response("files.read", {}), "done"]),
        _tool("files.read", chatty),
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED
    notes = [e for e in await runtime.events.history(result.run_id) if e.type == "tool.note"]
    assert len(notes) == MAX_TOOL_NOTES_PER_CALL and len(raised) == 1


async def test_only_the_conversation_owner_can_steer_a_conversation_turn_run() -> None:
    runtime = _runtime(MockModelProvider(["done"]))
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
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    request = _request().model_copy(update={"conversation_id": "c1"})
    result = await runtime.run(request)
    headers = {"x-tenant-id": "t", "x-scopes": "runs:read runs:write"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        url = f"/v1/runs/{result.run_id}/steer"
        other = await client.post(url, headers={**headers, "x-user-id": "bob"}, json={"text": "x"})
        # Another user of the tenant is told the run does not exist, before any steering check.
        assert other.status_code == 404
        owner = await client.post(url, headers={**headers, "x-user-id": "u"}, json={"text": "x"})
        assert owner.status_code == 409  # the run has ended
        read = await client.get(f"/v1/runs/{result.run_id}", headers={**headers, "x-user-id": "u"})
        assert "pending_steering" not in read.json()

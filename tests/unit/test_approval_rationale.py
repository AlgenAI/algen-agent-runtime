"""The approver sees why the agent wants to make the call (#120)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from conftest import make_agent, make_runtime
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.approvals.service import MAX_RATIONALE_CHARS
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.conversations.contracts import ConversationPendingAction
from algen_agent_runtime.conversations.service import (
    ConversationHandlerRegistry,
    ConversationService,
    RuntimeConversationHandler,
)
from algen_agent_runtime.conversations.stores import (
    InMemoryConversationEventBus,
    InMemoryConversationStore,
)
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import Container
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.types.contracts import Message, Role, RunRequest, RunState, RunStatus


def _reply(text: str) -> Any:
    response = tool_call_response("api.write", {"value": "draft"})
    return response.model_copy(update={"message": Message.text(Role.ASSISTANT, text)})


def _runtime(text: str) -> AgentRuntime:
    provider = MockModelProvider([_reply(text), "saved"])
    runtime = make_runtime(provider, make_agent(enabled_tools=frozenset({"api.write"})))

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
                idempotency="keyed",
            ),
            write,
        )
    )
    return runtime


async def _paused(runtime: AgentRuntime) -> RunState:
    state = await runtime.start(
        RunRequest(agent="test-agent", input="update it", tenant_id="tenant", user_id="user")
    )
    for _ in range(400):
        state = await runtime.status(state.id, "tenant")
        if state.status == RunStatus.AWAITING_APPROVAL:
            return state
        await asyncio.sleep(0.005)
    raise AssertionError(state.status)


async def test_the_approval_shows_the_models_text_redacted() -> None:
    runtime = _runtime("The record is stale,\n so I will  update it. token=abcdefghijkl")
    paused = await _paused(runtime)
    approval = await runtime.approvals.get(paused.pause_payload["approval_id"], "tenant")

    expected = "The record is stale, so I will update it. token=[REDACTED]"
    assert approval is not None and approval.rationale == expected
    assert paused.pause_payload["rationale"] == expected
    [event] = [e for e in await runtime.events.history(paused.id) if e.type == "approval.required"]
    assert event.data["rationale"] == expected
    await runtime.shutdown(grace_seconds=5)


async def test_a_long_rationale_is_capped_and_none_without_text() -> None:
    runtime = _runtime("why " * 400)
    paused = await _paused(runtime)
    rationale = paused.pause_payload["rationale"]
    assert len(rationale) == MAX_RATIONALE_CHARS and rationale.endswith("…")
    await runtime.shutdown(grace_seconds=5)

    silent = _runtime("")
    paused = await _paused(silent)
    assert paused.pause_payload["rationale"] is None
    await silent.shutdown(grace_seconds=5)


async def _span_attributes(include_content: bool) -> list[dict[str, Any]]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime = _runtime("Because the user asked.")
    runtime._tracer = provider.get_tracer("test")
    runtime.telemetry_include_content = include_content
    await _paused(runtime)
    await runtime.shutdown(grace_seconds=5)
    return [dict(span.attributes or {}) for span in exporter.get_finished_spans()]


async def test_the_rationale_stays_out_of_spans_unless_content_telemetry_is_on() -> None:
    off = await _span_attributes(False)
    assert not any("Because the user asked" in str(attributes) for attributes in off)
    on = await _span_attributes(True)
    assert any(a.get("approval.rationale") == "Because the user asked." for a in on)


def test_the_conversation_pending_action_carries_the_rationale() -> None:
    action = ConversationPendingAction(kind="approval", turn_id="t1", rationale="Because.")
    assert action.model_dump()["rationale"] == "Because."
    assert "rationale" not in ConversationPendingAction(kind="input", turn_id="t1").model_dump()


async def test_the_approval_api_lists_the_rationale() -> None:
    runtime = _runtime("Updating the stale record.")
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
    await _paused(runtime)
    headers = {"x-tenant-id": "tenant", "x-user-id": "user", "x-scopes": "approvals:read"}
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        [item] = (await client.get("/v1/approvals", headers=headers)).json()
    assert item["rationale"] == "Updating the stale record."
    assert item["tool"] == "api.write" and item["tool_fingerprint"]
    await runtime.shutdown(grace_seconds=5)

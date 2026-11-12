"""core.ask_user: the model asks the user and the run waits for the answer (A5 / G56)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.conversations import (
    ConversationHandlerRegistry,
    ConversationInputAnswer,
    ConversationService,
    MessageStatus,
    RuntimeConversationHandler,
)
from algen_agent_runtime.conversations.stores import (
    InMemoryConversationEventBus,
    InMemoryConversationStore,
)
from algen_agent_runtime.exceptions.errors import ToolExecutionError
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import Container
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.ask_user import ASK_USER_PERMISSION, ASK_USER_TOOL, ask_user_tool
from algen_agent_runtime.tools.contracts import Idempotency, SideEffect
from algen_agent_runtime.tools.testing import fake_tool_context
from algen_agent_runtime.types.contracts import Role, RunRequest, RunState, RunStatus, ToolPolicy

ASK = tool_call_response(ASK_USER_TOOL, {"question": "Which account?", "choices": ["main", "test"]})
ENABLED = {
    "enabled_tools": frozenset({ASK_USER_TOOL}),
    "tool_permissions": frozenset({ASK_USER_PERMISSION}),
}


def _runtime(*responses: Any, **agent: Any) -> tuple[AgentRuntime, MockModelProvider]:
    provider = MockModelProvider(list(responses))
    return make_runtime(provider, make_agent(**{**ENABLED, **agent})), provider


def _request(**updates: Any) -> RunRequest:
    return RunRequest(agent="test-agent", input="check it", tenant_id="t", user_id="u", **updates)


def _answers(state: RunState) -> list[dict[str, Any]]:
    return [
        json.loads(message.text_content)
        for message in state.messages
        if message.role == Role.TOOL and message.name == ASK_USER_TOOL
    ]


def test_the_definition_has_no_side_effect_and_is_never_cached() -> None:
    definition = ask_user_tool().definition

    assert definition.name == "core.ask_user"
    assert definition.required_permissions == {"user.ask"}
    assert definition.side_effect == SideEffect.NONE
    assert definition.idempotency == Idempotency.NON_IDEMPOTENT
    assert definition.volatile
    assert not definition.requires_approval


def test_every_runtime_registers_it() -> None:
    runtime, _ = _runtime()

    assert runtime.tools.get(ASK_USER_TOOL).definition.name == ASK_USER_TOOL


async def test_outside_a_run_it_refuses_to_run() -> None:
    with pytest.raises(ToolExecutionError, match="only inside an agent run"):
        await ask_user_tool().handler({"question": "?"}, fake_tool_context())


async def test_a_react_run_pauses_for_the_question_and_continues_with_the_answer() -> None:
    runtime, provider = _runtime(ASK, "Account main is up to date.")

    paused = await runtime.run(_request())

    assert paused.status == RunStatus.AWAITING_CLARIFICATION, paused.error
    state = await runtime.status(paused.run_id, "t")
    pause = state.pause_payload or {}
    assert pause["kind"] == "ask_user"
    assert (pause["question"], pause["choices"]) == ("Which account?", ["main", "test"])
    events = await runtime.events.history(state.id)
    assert [event.type for event in events[-2:]] == ["tool.started", "clarification.required"]
    assert events[-1].data["tool_call_id"] == pause["tool_call_id"]

    await runtime.resume(state.id, "t", {"clarification": "main"})
    final = await runtime.wait(state.id, "t")

    assert final.status == RunStatus.COMPLETED, final.error
    assert final.output == "Account main is up to date."
    done = await runtime.status(state.id, "t")
    assert _answers(done) == [{"answer": "main"}]
    # The answer reaches the model as the call's result, not as a new user turn.
    last = provider.requests[-1].messages[-1]
    assert last.role == Role.TOOL and json.loads(last.text_content) == {"answer": "main"}
    completed = [e for e in await runtime.events.history(state.id) if e.type == "tool.completed"]
    assert completed[-1].data["tool"] == ASK_USER_TOOL
    assert done.summary.tool_calls == 1


async def test_an_answer_outside_the_choices_is_taken_as_free_text() -> None:
    runtime, _ = _runtime(ASK, "ok")
    paused = await runtime.run(_request())

    await runtime.resume(paused.run_id, "t", {"clarification": "the shared one"})
    await runtime.wait(paused.run_id, "t")

    assert _answers(await runtime.status(paused.run_id, "t")) == [{"answer": "the shared one"}]


async def test_an_empty_answer_is_refused() -> None:
    runtime, _ = _runtime(ASK)
    paused = await runtime.run(_request())

    with pytest.raises(ValueError, match="clarification is required"):
        await runtime.resume(paused.run_id, "t", {"clarification": "  "})

    assert (await runtime.status(paused.run_id, "t")).status == RunStatus.AWAITING_CLARIFICATION


async def test_the_model_can_ask_twice() -> None:
    runtime, _ = _runtime(ASK, ASK, "done")
    paused = await runtime.run(_request())
    await runtime.resume(paused.run_id, "t", {"clarification": "main"})
    again = await runtime.wait(paused.run_id, "t")
    assert again.status == RunStatus.AWAITING_CLARIFICATION

    await runtime.resume(paused.run_id, "t", {"clarification": "test"})
    final = await runtime.wait(paused.run_id, "t")

    assert final.status == RunStatus.COMPLETED, final.error
    assert _answers(await runtime.status(paused.run_id, "t")) == [
        {"answer": "main"},
        {"answer": "test"},
    ]


async def test_an_agent_that_did_not_enable_it_cannot_ask() -> None:
    runtime, _ = _runtime(
        ASK, "carried on", enabled_tools=frozenset(), tool_permissions=frozenset()
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.FAILED
    assert "not enabled" in (result.error or "")


async def test_without_the_permission_the_call_is_refused() -> None:
    runtime, _ = _runtime(
        ASK,
        "carried on",
        tool_permissions=frozenset(),
        tool_policy=ToolPolicy(on_error="return_to_model"),
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED, result.error
    state = await runtime.status(result.run_id, "t")
    [refused] = [m for m in state.messages if m.role == Role.TOOL]
    assert "user.ask" in refused.text_content


async def test_invalid_arguments_are_refused_before_asking() -> None:
    runtime, _ = _runtime(
        tool_call_response(ASK_USER_TOOL, {"question": ""}),
        "carried on",
        tool_policy=ToolPolicy(on_error="return_to_model"),
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED, result.error


# HTTP ---------------------------------------------------------------------------------------


def _container(runtime: AgentRuntime) -> Container:
    handlers = ConversationHandlerRegistry()
    handlers.register(RuntimeConversationHandler(runtime))
    return Container(
        runtime=runtime,
        agents=runtime.agents,
        tools=runtime.tools,
        artifacts=InMemoryArtifactStore(),
        events=runtime.events,
        conversations=ConversationService(
            InMemoryConversationStore(), InMemoryConversationEventBus(), handlers
        ),
    )


async def test_over_http_the_question_is_read_and_answered_on_the_clarification_route() -> None:
    runtime, _ = _runtime(ASK, "Account main is up to date.")
    headers = {"x-tenant-id": "t", "x-user-id": "u", "x-scopes": "runs:read runs:write"}
    transport = httpx.ASGITransport(app=create_app(AppSettings(), _container(runtime)))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started = await client.post(
            "/v1/runs", headers=headers, json={"agent": "test-agent", "input": "check it"}
        )
        run_id = started.json()["run_id"]
        await runtime.wait(run_id, "t")

        read = (await client.get(f"/v1/runs/{run_id}", headers=headers)).json()
        assert read["status"] == "awaiting_clarification"
        assert read["pause_payload"]["question"] == "Which account?"
        assert read["pause_payload"]["choices"] == ["main", "test"]
        events = await client.get(f"/v1/runs/{run_id}/events", headers=headers)
        assert '"clarification.required"' in events.text.strip().split("\n\n")[-1]

        other = {**headers, "x-user-id": "mallory"}
        denied = await client.post(
            f"/v1/runs/{run_id}/clarification", headers=other, json={"clarification": "test"}
        )
        assert denied.status_code == 403
        answered = await client.post(
            f"/v1/runs/{run_id}/clarification", headers=headers, json={"clarification": "main"}
        )
        assert answered.status_code == 202, answered.text

    final = await runtime.wait(run_id, "t")
    assert final.status == RunStatus.COMPLETED, final.error
    await runtime.shutdown(grace_seconds=5)


# Conversations ------------------------------------------------------------------------------


async def test_a_conversation_turn_waits_for_input_with_the_choices() -> None:
    runtime, _ = _runtime(ASK, "Account main is up to date.")
    service = _container(runtime).conversations
    conversation = await service.create(tenant_id="t", user_id="u", agent="test-agent")
    await service.submit(conversation.id, "t", "u", "is my account up to date?")
    for _ in range(400):
        [*_, message] = await service.messages(conversation.id, "t")
        if message.status == MessageStatus.AWAITING_INPUT:
            break
        await asyncio.sleep(0.005)
    assert message.status == MessageStatus.AWAITING_INPUT, message.status
    assert message.pending is not None
    assert (message.pending.kind, message.pending.question, message.pending.choices) == (
        "input",
        "Which account?",
        ("main", "test"),
    )
    assert message.text_content == "Which account?"

    await service.respond(
        conversation.id, message.pending.turn_id, "t", "u", ConversationInputAnswer(text="main")
    )
    for _ in range(400):
        [*_, done] = await service.messages(conversation.id, "t")
        if done.status == MessageStatus.COMPLETED:
            break
        await asyncio.sleep(0.005)

    assert done.id == message.id
    assert done.status == MessageStatus.COMPLETED
    assert done.text_content == "Account main is up to date."

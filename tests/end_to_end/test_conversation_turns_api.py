"""HTTP routes for answering paused conversation turns."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from conftest import make_agent, make_runtime

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
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import Container
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition

SCOPES = "conversations:read conversations:write"


def headers(tenant: str = "tenant", user: str = "user", scopes: str = SCOPES) -> dict[str, str]:
    return {"x-tenant-id": tenant, "x-user-id": user, "x-scopes": scopes}


def make_container(calls: list[dict[str, Any]]) -> Container:
    provider = MockModelProvider([tool_call_response("api.write", {"value": "draft"}), "saved"] * 2)
    runtime = make_runtime(provider, make_agent(enabled_tools=frozenset({"api.write"})))

    async def write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        calls.append({"arguments": dict(arguments), "credentials": dict(context.credentials)})
        return {"ok": True}

    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="api.write",
                version="1",
                description="Write a record.",
                input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
                output_schema={"type": "object"},
                side_effect=SideEffect.WRITE,
                idempotency="keyed",
            ),
            write,
        )
    )
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


async def last_message(client: httpx.AsyncClient, conversation_id: str, *statuses: str) -> Any:
    for _ in range(400):
        response = await client.get(
            f"/v1/conversations/{conversation_id}/messages", headers=headers()
        )
        message = response.json()[-1]
        if message["status"] in statuses:
            return message
        await asyncio.sleep(0.005)
    raise AssertionError(f"message never reached {statuses}: {message['status']}")


async def test_approval_route_resumes_the_turn_with_isolation_and_conflicts() -> None:
    calls: list[dict[str, Any]] = []
    transport = httpx.ASGITransport(app=create_app(AppSettings(), make_container(calls)))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/conversations", headers=headers(), json={"agent": "test-agent"}
        )
        conversation_id = created.json()["id"]
        accepted = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers={**headers(), "authorization": "Bearer not-forwarded"},
            json={"text": "update it"},
        )
        turn_id = accepted.json()["message_id"]
        paused = await last_message(client, conversation_id, "awaiting_approval")
        assert paused["pending"]["turn_id"] == turn_id
        assert paused["pending"]["tool"] == "api.write"

        second = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers(),
            json={"text": "another"},
        )
        assert second.status_code == 409
        assert turn_id in second.json()["detail"]

        route = f"/v1/conversations/{conversation_id}/turns/{turn_id}/approval"
        for denied, code in (
            (headers(tenant="other"), 404),
            (headers(user="intruder"), 404),
            (headers(scopes="conversations:read"), 403),
        ):
            response = await client.post(route, headers=denied, json={"decision": "approved"})
            assert response.status_code == code, response.text
        invalid = await client.post(route, headers=headers(), json={"decision": "modified"})
        assert invalid.status_code == 422
        wrong_kind = await client.post(
            f"/v1/conversations/{conversation_id}/turns/{turn_id}/input",
            headers=headers(),
            json={"text": "hello"},
        )
        assert wrong_kind.status_code == 409
        assert calls == []

        approved = await client.post(
            route,
            headers={**headers(), "authorization": "Bearer not-forwarded"},
            json={"decision": "modified", "modified_parameters": {"value": "final"}},
        )
        assert approved.status_code == 202, approved.text
        assert approved.json() == {
            "turn_id": turn_id,
            "assistant_message_id": paused["id"],
            "status": "streaming",
        }
        done = await last_message(client, conversation_id, "completed", "failed")
        assert done["id"] == paused["id"]
        assert done["status"] == "completed"
        assert done["content"] == [{"type": "text", "text": "saved"}]
        # Headers are never forwarded to tools as credentials.
        assert calls == [{"arguments": {"value": "final"}, "credentials": {}}]

        again = await client.post(route, headers=headers(), json={"decision": "approved"})
        assert again.status_code == 409


async def test_cancel_route_ends_a_pending_turn() -> None:
    calls: list[dict[str, Any]] = []
    transport = httpx.ASGITransport(app=create_app(AppSettings(), make_container(calls)))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/conversations", headers=headers(), json={"agent": "test-agent"}
        )
        conversation_id = created.json()["id"]
        await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers(),
            json={"text": "update it"},
        )
        await last_message(client, conversation_id, "awaiting_approval")

        cancelled = await client.delete(
            f"/v1/conversations/{conversation_id}/active-response", headers=headers()
        )
        assert cancelled.status_code == 202
        done = await last_message(client, conversation_id, "cancelled")
        assert done["pending"] is None
        accepted = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers(),
            json={"text": "next"},
        )
        assert accepted.status_code == 202
        assert calls == []

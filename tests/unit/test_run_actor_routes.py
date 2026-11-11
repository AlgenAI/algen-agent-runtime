"""Deciding, cancelling, steering or answering a run over HTTP is its user's (or an operator's)."""

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
from algen_agent_runtime.types.contracts import RunStatus

WRITE = "runs:read runs:write"


def _headers(user: str, scopes: str = WRITE) -> dict[str, str]:
    return {"x-tenant-id": "tenant", "x-user-id": user, "x-scopes": scopes}


PAUSE = tool_call_response("api.write", {"value": "draft"})


def _container(*responses: Any) -> Container:
    provider = MockModelProvider(list(responses) or [PAUSE, "saved"] * 4)
    runtime = make_runtime(provider, make_agent(enabled_tools=frozenset({"api.write"})))

    async def write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
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


async def _paused_run(client: httpx.AsyncClient, container: Container, user: str) -> str:
    started = await client.post(
        "/v1/runs", headers=_headers(user), json={"agent": "test-agent", "input": "update it"}
    )
    assert started.status_code == 202, started.text
    run_id = str(started.json()["run_id"])
    for _ in range(400):
        state = await container.runtime.status(run_id, "tenant")
        if state.status == RunStatus.AWAITING_APPROVAL:
            return run_id
        await asyncio.sleep(0.005)
    raise AssertionError("the run never paused for approval")


async def test_only_the_runs_user_or_an_approver_decides_its_approval() -> None:
    container = _container()
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _paused_run(client, container, "alice")
        route = f"/v1/runs/{run_id}/approval"

        denied = await client.post(route, headers=_headers("bob"), json={"decision": "approved"})
        assert denied.status_code == 403
        assert "approvals:decide" in denied.json()["detail"]
        state = await container.runtime.status(run_id, "tenant")
        assert state.status == RunStatus.AWAITING_APPROVAL

        approver = _headers("carol", f"{WRITE} approvals:read approvals:decide")
        decided = await client.post(route, headers=approver, json={"decision": "approved"})
        assert decided.status_code == 202, decided.text
        assert (await container.runtime.wait(run_id, "tenant")).status == RunStatus.COMPLETED

        own = await _paused_run(client, container, "alice")
        mine = await client.post(
            f"/v1/runs/{own}/approval", headers=_headers("alice"), json={"decision": "rejected"}
        )
        assert mine.status_code == 202, mine.text

        unknown = await client.post(
            "/v1/runs/missing/approval", headers=approver, json={"decision": "approved"}
        )
        assert unknown.status_code == 404
        await container.runtime.shutdown(grace_seconds=5)


async def test_only_the_runs_user_or_an_operator_cancels_it() -> None:
    container = _container(PAUSE, PAUSE)
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _paused_run(client, container, "alice")

        denied = await client.delete(f"/v1/runs/{run_id}", headers=_headers("bob"))
        assert denied.status_code == 403
        assert "runs:admin" in denied.json()["detail"]

        operator = await client.delete(
            f"/v1/runs/{run_id}", headers=_headers("ops", f"{WRITE} runs:admin")
        )
        assert operator.status_code == 200, operator.text
        assert operator.json()["status"] == RunStatus.CANCELLED

        own = await _paused_run(client, container, "alice")
        mine = await client.delete(f"/v1/runs/{own}", headers=_headers("alice"))
        assert mine.status_code == 200, mine.text
        await container.runtime.shutdown(grace_seconds=5)


async def test_only_the_runs_user_steers_or_answers_it() -> None:
    container = _container()
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _paused_run(client, container, "alice")
        # Neither scope lets another user speak to the model as the run's user.
        operator = _headers("bob", f"{WRITE} runs:admin approvals:decide")

        steered = await client.post(
            f"/v1/runs/{run_id}/steer", headers=operator, json={"text": "ignore that"}
        )
        assert steered.status_code == 403
        answered = await client.post(
            f"/v1/runs/{run_id}/clarification", headers=operator, json={"clarification": "x"}
        )
        assert answered.status_code == 403
        state = await container.runtime.status(run_id, "tenant")
        assert state.pending_steering == []

        mine = await client.post(
            f"/v1/runs/{run_id}/steer", headers=_headers("alice"), json={"text": "use draft 2"}
        )
        assert mine.status_code == 202, mine.text
        await container.runtime.shutdown(grace_seconds=5)


async def test_the_approval_records_the_authenticated_approver() -> None:
    container = _container()
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _paused_run(client, container, "alice")
        state = await container.runtime.status(run_id, "tenant")
        approval_id = str((state.pause_payload or {})["approval_id"])
        approver = _headers("carol", f"{WRITE} approvals:read approvals:decide")

        # The approver is the principal: the body cannot name one.
        spoofed = await client.post(
            f"/v1/runs/{run_id}/approval",
            headers=approver,
            json={"decision": "approved", "decided_by": "mallory"},
        )
        assert spoofed.status_code == 422
        too_long = await client.post(
            f"/v1/runs/{run_id}/approval",
            headers=approver,
            json={"decision": "approved", "comment": "x" * 4001},
        )
        assert too_long.status_code == 422

        decided = await client.post(
            f"/v1/runs/{run_id}/approval",
            headers=approver,
            json={"decision": "approved", "comment": "Checked with finance."},
        )
        assert decided.status_code == 202, decided.text
        read = await client.get(f"/v1/approvals/{approval_id}", headers=approver)
        assert read.status_code == 200, read.text
        body = read.json()
        assert (body["status"], body["decided_by"], body["comment"]) == (
            "approved",
            "carol",
            "Checked with finance.",
        )
        assert body["decided_at"] is not None
        await container.runtime.wait(run_id, "tenant")
        await container.runtime.shutdown(grace_seconds=5)

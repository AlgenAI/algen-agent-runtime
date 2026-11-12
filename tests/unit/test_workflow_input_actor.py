"""Answering a workflow run's question over HTTP is its user's only (0.2.0b3)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.types.contracts import utc_now
from algen_agent_runtime.workflows.contracts import (
    WorkflowExecutionState,
    WorkflowNodeExecution,
    WorkflowNodeStatus,
    WorkflowStatus,
)
from algen_agent_runtime.workflows.service import OWNER as SERVICE_OWNER

httpx = pytest.importorskip("httpx")


def headers(user: str, scopes: str = "runs:read runs:write") -> dict[str, str]:
    return {"x-tenant-id": "acme", "x-user-id": user, "x-scopes": scopes}


OWNER = headers("clerk")


def settings() -> AppSettings:
    return AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
            "api": {"rate_limiting": {"enabled": False}},
            "agents": [
                {
                    "name": "worker",
                    "version": "1.0.0",
                    "description": "Works",
                    "system_instructions": "Do the work.",
                    "default_model": {"name": "d", "provider": "mock", "model": "deterministic"},
                }
            ],
            "workflows": {
                "report": {
                    "name": "report",
                    "version": "1.0.0",
                    "output_key": "report",
                    "nodes": [
                        {
                            "id": "work",
                            "kind": "agent",
                            "agent": "worker",
                            "output_key": "report",
                            "input_template": "{{input}}",
                        }
                    ],
                }
            },
        }
    )


async def waiting_run(app: Any, workflow_id: str) -> str:
    """A run of ``clerk``'s whose step asked which region to report on."""
    await app.state.container.workflow_checkpoints.create(
        WorkflowExecutionState(
            id=workflow_id,
            manifest_name="report",
            manifest_version="1.0.0",
            tenant_id="acme",
            user_id="clerk",
            owner=SERVICE_OWNER,
            status=WorkflowStatus.AWAITING_INPUT,
            pause={"node_id": "work", "question": "Which region?", "requested_at": "now"},
            values={"input": "the sales report"},
            nodes={
                "work": WorkflowNodeExecution(
                    node_id="work", status=WorkflowNodeStatus.PENDING, started_at=utc_now()
                )
            },
        )
    )
    return f"/v1/workflows/runs/{workflow_id}/input"


async def status(client: Any, workflow_id: str) -> str:
    found = await client.get(f"/v1/workflows/runs/{workflow_id}", headers=OWNER)
    return str(found.json()["status"])


async def until_not_waiting(client: Any, workflow_id: str) -> str:
    for _ in range(500):
        current = await status(client, workflow_id)
        if current != "awaiting_input":
            return current
        await asyncio.sleep(0.01)
    raise AssertionError("the run is still waiting for input")


@pytest.mark.parametrize(
    "answerer",
    [
        headers("someone-else"),  # another user of the tenant
        headers("svc-uploader"),  # a service token with runs:write
        headers("approver", "runs:write approvals:decide"),  # deciding is not answering
        headers("operator", "runs:write runs:admin"),  # nor is cancelling
    ],
)
async def test_only_the_runs_user_answers_its_question(answerer: dict[str, str]) -> None:
    from algen_agent_runtime.api.app import create_app

    app = create_app(settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        answer = await waiting_run(app, "report-0001")
        refused = await client.post(answer, json={"values": {"region": "EU"}}, headers=answerer)
        assert refused.status_code == 403, refused.text
        assert "only the workflow run's user may answer it" in refused.json()["detail"]
        assert await status(client, "report-0001") == "awaiting_input"
        state = await app.state.container.workflow_checkpoints.get("report-0001", "acme")
        assert "region" not in state.values

        accepted = await client.post(answer, json={"values": {"region": "EU"}}, headers=OWNER)
        assert accepted.status_code == 202, accepted.text
        assert await until_not_waiting(client, "report-0001") != "awaiting_input"
        state = await app.state.container.workflow_checkpoints.get("report-0001", "acme")
        assert state.values["region"] == "EU"


async def test_another_tenant_still_gets_404() -> None:
    from algen_agent_runtime.api.app import create_app

    app = create_app(settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        answer = await waiting_run(app, "report-0002")
        other = {**headers("clerk"), "x-tenant-id": "other"}
        response = await client.post(answer, json={"values": {"region": "EU"}}, headers=other)
        assert response.status_code == 404

"""Deciding a workflow run's approval over HTTP is its user's, or an approver's (0.2.0b3)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from algen_agent_runtime.config.settings import AppSettings

httpx = pytest.importorskip("httpx")

OWNER = {"x-tenant-id": "acme", "x-user-id": "clerk", "x-scopes": "runs:read runs:write"}


def headers(user: str, scopes: str = "runs:read runs:write") -> dict[str, str]:
    return {"x-tenant-id": "acme", "x-user-id": user, "x-scopes": scopes}


def settings() -> AppSettings:
    return AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
            "api": {"rate_limiting": {"enabled": False}},
            "workflows": {
                "review": {
                    "name": "review",
                    "version": "1.0.0",
                    "output_key": "approval",
                    "nodes": [
                        {
                            "id": "review",
                            "kind": "approval",
                            "output_key": "approval",
                            "approval": {"prompt": "Publish it?"},
                        }
                    ],
                }
            },
        }
    )


async def until(client: Any, workflow_id: str, status: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for _ in range(500):
        found = (await client.get(f"/v1/workflows/runs/{workflow_id}", headers=OWNER)).json()
        if found.get("status") == status:
            return found
        await asyncio.sleep(0.01)
    raise AssertionError(f"run never reached {status}: {found}")


async def waiting_run(client: Any, workflow_id: str) -> str:
    started = await client.post(
        "/v1/workflows/review/runs",
        json={"input": "the release notes", "workflow_id": workflow_id},
        headers=OWNER,
    )
    assert started.status_code == 202, started.text
    await until(client, workflow_id, "awaiting_approval")
    return f"/v1/workflows/runs/{workflow_id}/approval"


async def decided_by(app: Any, workflow_id: str) -> str:
    state = await app.state.container.workflow_checkpoints.get(workflow_id, "acme")
    return str(state.values["approval"]["decided_by"])


@pytest.mark.parametrize(
    ("decider", "allowed"),
    [
        (headers("clerk"), True),  # the run's own user
        (headers("approver", "runs:write approvals:decide"), True),  # an approver
        (headers("someone-else"), False),  # another user without the scope
        (headers("svc-uploader", "runs:read runs:write"), False),  # a service token
    ],
)
async def test_only_the_runs_user_or_an_approver_decides_its_approval(
    decider: dict[str, str], allowed: bool
) -> None:
    from algen_agent_runtime.api.app import create_app

    app = create_app(settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        approval = await waiting_run(client, "release-0001")
        response = await client.post(approval, json={"decision": "approved"}, headers=decider)
        if not allowed:
            assert response.status_code == 403, response.text
            assert "approvals:decide" in response.json()["detail"]
            assert (await until(client, "release-0001", "awaiting_approval"))["pause"]
            return
        assert response.status_code == 202, response.text
        await until(client, "release-0001", "completed")
        # The decider is the authenticated principal.
        assert await decided_by(app, "release-0001") == decider["x-user-id"]


async def test_the_body_cannot_name_another_decider() -> None:
    from algen_agent_runtime.api.app import create_app

    app = create_app(settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        approval = await waiting_run(client, "release-0002")
        refused = await client.post(
            approval, json={"decision": "approved", "decided_by": "ceo"}, headers=OWNER
        )
        assert refused.status_code == 422
        accepted = await client.post(approval, json={"decision": "approved"}, headers=OWNER)
        assert accepted.status_code == 202
        await until(client, "release-0002", "completed")
        assert await decided_by(app, "release-0002") == "clerk"


async def test_another_tenant_still_gets_404() -> None:
    from algen_agent_runtime.api.app import create_app

    app = create_app(settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        approval = await waiting_run(client, "release-0003")
        other = {**headers("clerk", "runs:write approvals:decide"), "x-tenant-id": "other"}
        response = await client.post(approval, json={"decision": "approved"}, headers=other)
        assert response.status_code == 404

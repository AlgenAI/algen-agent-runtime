"""Listing approval requests: the store contract, the PostgreSQL query and the HTTP routes."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import httpx
import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.approvals.service import (
    ApprovalRequest,
    ApprovalStatus,
    InMemoryApprovalService,
    PostgresApprovalService,
)
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
from algen_agent_runtime.types.contracts import RunRequest, RunStatus, utc_now


async def _seed(service: InMemoryApprovalService) -> dict[str, ApprovalRequest]:
    created = {
        "a1": await service.create("run-a", "s1", "tenant-a", "Write", {}, "write", 600, agent="a"),
        "a2": await service.create("run-a", "s2", "tenant-a", "Send", {}, "external", 600),
        "b1": await service.create("run-b", "s1", "tenant-a", "Delete", {}, "high", 600, agent="b"),
        "other": await service.create("run-c", "s1", "tenant-b", "Write", {}, "write", 600),
    }
    return created


async def test_memory_list_is_tenant_scoped_newest_first_and_filtered() -> None:
    service = InMemoryApprovalService()
    created = await _seed(service)

    listed = await service.list("tenant-a")
    assert [item.id for item in listed] == [created[k].id for k in ("b1", "a2", "a1")]
    assert all(item.tenant_id == "tenant-a" for item in listed)
    assert created["a1"].agent == "a"
    assert created["a1"].created_at is not None

    by_run = await service.list("tenant-a", run_id="run-a")
    assert {item.id for item in by_run} == {created["a1"].id, created["a2"].id}
    by_agent = await service.list("tenant-a", agent="b")
    assert [item.id for item in by_agent] == [created["b1"].id]
    assert await service.list("tenant-b", run_id="run-a") == ()

    first = await service.list("tenant-a", limit=2)
    second = await service.list("tenant-a", limit=2, offset=2)
    assert [item.id for item in (*first, *second)] == [item.id for item in listed]


async def test_memory_list_reports_status_and_expiry_as_a_reader_sees_it() -> None:
    service = InMemoryApprovalService()
    created = await _seed(service)
    await service.decide(created["a2"].id, "tenant-a", ApprovalStatus.APPROVED)
    lapsed = await service.create("run-d", "s1", "tenant-a", "Old", {}, "write", 1)
    service._items[lapsed.id].expires_at = utc_now() - timedelta(seconds=1)

    pending = await service.list("tenant-a", status=ApprovalStatus.PENDING)
    assert {item.id for item in pending} == {created["a1"].id, created["b1"].id}
    approved = await service.list("tenant-a", status=ApprovalStatus.APPROVED)
    assert [item.id for item in approved] == [created["a2"].id]
    expired = await service.list("tenant-a", status=ApprovalStatus.EXPIRED)
    assert [(item.id, item.status) for item in expired] == [(lapsed.id, ApprovalStatus.EXPIRED)]
    # Reading never changes the stored request.
    assert service._items[lapsed.id].status == ApprovalStatus.PENDING


async def test_list_returns_copies_and_validates_paging() -> None:
    service = InMemoryApprovalService()
    created = await _seed(service)

    listed = await service.list("tenant-a", agent="a")
    listed[0].redacted_parameters["changed"] = True
    assert "changed" not in service._items[created["a1"].id].redacted_parameters
    for kwargs in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
        with pytest.raises(ValueError):
            await service.list("tenant-a", **kwargs)


class _RecordingDatabase:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append((query, args))
        return self.rows


def _stored(**updates: Any) -> ApprovalRequest:
    values: dict[str, Any] = {
        "run_id": "run-1",
        "step_id": "step-1",
        "tenant_id": "tenant-a",
        "proposed_action": "Write",
        "side_effect_summary": "Proposed write operation: Write",
        "redacted_parameters": {},
        "risk": "write",
        "expires_at": utc_now() + timedelta(minutes=5),
    }
    values.update(updates)
    return ApprovalRequest(**values)


async def test_postgres_list_binds_every_filter_and_decodes_json_rows() -> None:
    created_at = utc_now() - timedelta(minutes=1)
    legacy = _stored()  # stored by an earlier release: no agent, no created_at
    lapsed = _stored(expires_at=utc_now() - timedelta(seconds=1), agent="a", created_at=created_at)
    database = _RecordingDatabase(
        [
            {"approval": legacy.model_dump_json(), "created_at": created_at},
            {"approval": lapsed.model_dump_json(), "created_at": created_at},
        ]
    )
    service = PostgresApprovalService(database)

    items = await service.list(
        "tenant-a", status=ApprovalStatus.PENDING, run_id="run-1", agent="a", limit=10, offset=20
    )

    query, args = database.queries[0]
    assert "FROM algen_agent_runtime_approvals WHERE tenant_id=$1" in query
    assert "run_id=$2" in query
    assert "approval->>'agent'=$3" in query
    assert "approval->>'status'='pending' AND NOT (approval->>'expires_at')::timestamptz <= $4" in (
        query
    )
    assert query.endswith("ORDER BY created_at DESC, id DESC LIMIT $5 OFFSET $6")
    assert args[:3] == ("tenant-a", "run-1", "a")
    assert args[4:] == (10, 20)
    assert items[0].created_at == created_at
    assert items[1].status == ApprovalStatus.EXPIRED


@pytest.mark.parametrize(
    ("status", "clause"),
    [
        (
            ApprovalStatus.EXPIRED,
            "(approval->>'status'='expired' OR (approval->>'status'='pending' AND "
            "(approval->>'expires_at')::timestamptz <= $2))",
        ),
        (ApprovalStatus.REJECTED, "approval->>'status'=$2"),
        (None, None),
    ],
)
async def test_postgres_list_status_clauses(status: ApprovalStatus | None, clause: str) -> None:
    database = _RecordingDatabase([])
    await PostgresApprovalService(database).list("tenant-a", status=status)

    query, args = database.queries[0]
    if clause is None:
        assert "status" not in query
        assert args == ("tenant-a", 50, 0)
    else:
        assert clause in query
    if status == ApprovalStatus.REJECTED:
        assert args[1] == "rejected"


SCOPES = "runs:read runs:write"


def _headers(tenant: str = "tenant", scopes: str = SCOPES) -> dict[str, str]:
    return {"x-tenant-id": tenant, "x-user-id": "user", "x-scopes": scopes}


def _container() -> Container:
    provider = MockModelProvider([tool_call_response("api.write", {"value": "draft"}), "saved"])
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


async def test_http_routes_list_and_read_the_runs_approvals() -> None:
    container = _container()
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started = await client.post(
            "/v1/runs", headers=_headers(), json={"agent": "test-agent", "input": "update it"}
        )
        run_id = started.json()["run_id"]
        for _ in range(400):
            state = await container.runtime.status(run_id, "tenant")
            if state.status == RunStatus.AWAITING_APPROVAL:
                break
            await asyncio.sleep(0.005)
        assert state.status == RunStatus.AWAITING_APPROVAL
        approval_id = state.pause_payload["approval_id"]

        listed = await client.get("/v1/approvals?status=pending", headers=_headers())
        assert listed.status_code == 200, listed.text
        [item] = listed.json()
        assert item["id"] == approval_id
        assert item["run_id"] == run_id
        assert item["agent"] == "test-agent"
        assert item["status"] == "pending"
        assert item["proposed_action"] == "Execute api.write"
        assert item["created_at"]

        by_run = await client.get(
            "/v1/approvals", params={"run_id": run_id, "agent": "test-agent"}, headers=_headers()
        )
        assert [entry["id"] for entry in by_run.json()] == [approval_id]
        assert (await client.get("/v1/approvals?agent=other", headers=_headers())).json() == []
        read = await client.get(f"/v1/approvals/{approval_id}", headers=_headers())
        assert read.status_code == 200
        assert read.json()["id"] == approval_id

        assert (await client.get("/v1/approvals", headers=_headers("other"))).json() == []
        missing = await client.get(f"/v1/approvals/{approval_id}", headers=_headers("other"))
        assert missing.status_code == 404
        denied = await client.get("/v1/approvals", headers=_headers(scopes="artifacts:read"))
        assert denied.status_code == 403
        for query in ("limit=0", "limit=501", "offset=-1", "status=unknown"):
            invalid = await client.get(f"/v1/approvals?{query}", headers=_headers())
            assert invalid.status_code == 422, query

        decided = await client.post(
            f"/v1/runs/{run_id}/approval", headers=_headers(), json={"decision": "approved"}
        )
        assert decided.status_code == 202, decided.text
        assert (await client.get("/v1/approvals?status=pending", headers=_headers())).json() == []
        approved = await client.get("/v1/approvals?status=approved", headers=_headers())
        assert [entry["id"] for entry in approved.json()] == [approval_id]
        await container.runtime.shutdown(grace_seconds=5)


async def test_runtime_records_the_agent_on_approval_requests() -> None:
    container = _container()
    state = await container.runtime.start(
        RunRequest(agent="test-agent", input="update it", tenant_id="tenant", user_id="user")
    )
    for _ in range(400):
        state = await container.runtime.status(state.id, "tenant")
        if state.status == RunStatus.AWAITING_APPROVAL:
            break
        await asyncio.sleep(0.005)

    [approval] = await container.runtime.approvals.list("tenant")
    assert approval.agent == "test-agent"
    assert approval.run_id == state.id

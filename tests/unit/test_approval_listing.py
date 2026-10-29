"""Listing approval requests: the store contract, the PostgreSQL query and the HTTP routes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from conftest import make_agent, make_runtime
from pydantic import BaseModel, ConfigDict

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
from algen_agent_runtime.security.redaction import redact_parameters
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
    for kwargs in ({"limit": 0}, {"limit": 501}, {"offset": -1}, {"offset": 1_000_001}):
        with pytest.raises(ValueError):
            await service.list("tenant-a", **kwargs)


class _RecordingDatabase:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append((query, args))
        return self.rows


def _row_json(item: ApprovalRequest) -> str:
    """The approval column as the PostgreSQL store writes it, and asyncpg returns it."""
    return item.model_dump_json(exclude={"agent", "created_at"})


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
    legacy = _stored()  # stored by an earlier release: no agent column
    lapsed = _stored(expires_at=utc_now() - timedelta(seconds=1))
    database = _RecordingDatabase(
        [
            {"approval": _row_json(legacy), "agent": None, "created_at": created_at},
            {"approval": _row_json(lapsed), "agent": "a", "created_at": created_at},
        ]
    )
    service = PostgresApprovalService(database)

    items = await service.list(
        "tenant-a", status=ApprovalStatus.PENDING, run_id="run-1", agent="a", limit=10, offset=20
    )

    query, args = database.queries[0]
    assert "FROM algen_agent_runtime_approvals WHERE tenant_id=$1" in query
    assert "run_id=$2" in query
    assert "agent=$3" in query
    assert "approval->>'agent'" not in query
    assert "approval->>'status'='pending' AND NOT (approval->>'expires_at')::timestamptz <= $4" in (
        query
    )
    assert query.endswith("ORDER BY created_at DESC, id DESC LIMIT $5 OFFSET $6")
    assert args[:3] == ("tenant-a", "run-1", "a")
    assert args[4:] == (10, 20)
    assert "SELECT approval, agent, created_at FROM" in query
    assert [(item.agent, item.created_at) for item in items] == [
        (None, created_at),
        ("a", created_at),
    ]
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


SCOPES = "runs:read runs:write approvals:read"


def _headers(tenant: str = "tenant", scopes: str = SCOPES, user: str = "user") -> dict[str, str]:
    return {"x-tenant-id": tenant, "x-user-id": user, "x-scopes": scopes}


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
        # runs:read is not enough: the inbox shows every user's pending actions.
        for scopes in ("artifacts:read", "runs:read runs:write"):
            denied = await client.get("/v1/approvals", headers=_headers(scopes=scopes))
            assert denied.status_code == 403, scopes
            denied = await client.get(
                f"/v1/approvals/{approval_id}", headers=_headers(scopes=scopes)
            )
            assert denied.status_code == 403, scopes
        for query in ("limit=0", "limit=501", "offset=-1", "offset=1000001", "status=unknown"):
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


class _A17ApprovalRequest(BaseModel):
    """``ApprovalRequest`` exactly as 0.1.0a17 validates the stored JSON."""

    model_config = ConfigDict(extra="forbid")
    id: str
    run_id: str
    step_id: str
    tenant_id: str
    proposed_action: str
    side_effect_summary: str
    redacted_parameters: dict[str, Any]
    risk: str
    expires_at: datetime
    status: ApprovalStatus = ApprovalStatus.PENDING
    modified_parameters: dict[str, Any] | None = None


class _ApprovalTable:
    """The approvals table: the JSON column as text (as asyncpg returns jsonb) and the columns."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.inserted: list[tuple[Any, ...]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if query.startswith("INSERT"):
            self.inserted.append(args)
            approval_id, _run_id, _tenant_id, agent, approval = args
            created_at = utc_now()
            self.rows[approval_id] = {
                "approval": approval,
                "agent": agent,
                "created_at": created_at,
            }
            return {"created_at": created_at}
        return self.rows.get(args[0])

    async def execute(self, query: str, *args: Any) -> str:
        approval, approval_id = args[0], args[1]
        self.rows[approval_id]["approval"] = approval
        return "UPDATE 1"


async def test_postgres_store_writes_json_that_0_1_0a17_can_read() -> None:
    table = _ApprovalTable()
    service = PostgresApprovalService(table)

    created = await service.create("run-1", "s1", "tenant-a", "Write", {}, "write", 600, agent="a")

    [inserted] = table.inserted
    assert inserted[3] == "a"  # the agent column
    stored = table.rows[created.id]
    _A17ApprovalRequest.model_validate_json(stored["approval"])
    assert created.created_at == stored["created_at"]
    read = await service.get(created.id, "tenant-a")
    assert read is not None
    assert (read.agent, read.created_at) == ("a", stored["created_at"])

    await service.decide(
        created.id, "tenant-a", ApprovalStatus.MODIFIED, modified_parameters={"value": "x"}
    )
    decided = _A17ApprovalRequest.model_validate_json(table.rows[created.id]["approval"])
    assert decided.status == ApprovalStatus.MODIFIED

    # A row written by 0.1.0a17 is read, and decided, in the same shape.
    legacy = _stored()
    table.rows[legacy.id] = {
        "approval": _A17ApprovalRequest.model_validate(
            legacy.model_dump(exclude={"agent", "created_at"})
        ).model_dump_json(),
        "agent": None,
        "created_at": utc_now(),
    }
    await service.decide(legacy.id, "tenant-a", ApprovalStatus.APPROVED)
    _A17ApprovalRequest.model_validate_json(table.rows[legacy.id]["approval"])


def test_redact_parameters_redacts_sensitive_top_level_keys() -> None:
    assert redact_parameters(
        {"value": "x", "API_KEY": "k", "token": "t", "nested": {"password": "p"}}
    ) == {"value": "x", "API_KEY": "[REDACTED]", "token": "[REDACTED]", "nested": {"password": "p"}}


async def _await_pending(container: Container, run_id: str | None = None) -> ApprovalRequest:
    for _ in range(400):
        pending = await container.runtime.approvals.list("tenant", status=ApprovalStatus.PENDING)
        matching = [item for item in pending if run_id is None or item.run_id == run_id]
        if matching:
            return matching[0]
        await asyncio.sleep(0.005)
    raise AssertionError("no pending approval")


async def test_http_routes_redact_modified_parameters() -> None:
    container = _container()
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started = await client.post(
            "/v1/runs", headers=_headers(), json={"agent": "test-agent", "input": "update it"}
        )
        run_id = started.json()["run_id"]
        approval = await _await_pending(container, run_id)

        decided = await client.post(
            f"/v1/runs/{run_id}/approval",
            headers=_headers(),
            json={
                "decision": "modified",
                "modified_parameters": {"value": "x", "api_key": "sk-live-123"},
            },
        )
        assert decided.status_code == 202, decided.text

        listed = await client.get("/v1/approvals", headers=_headers())
        read = await client.get(f"/v1/approvals/{approval.id}", headers=_headers())
        for response in (listed, read):
            assert response.status_code == 200
            assert "sk-live-123" not in response.text
        [item] = listed.json()
        assert item["status"] == "modified"
        assert item["modified_parameters"] == {"value": "x", "api_key": "[REDACTED]"}
        assert read.json()["modified_parameters"] == item["modified_parameters"]
        await container.runtime.shutdown(grace_seconds=5)


async def test_another_user_cannot_list_or_decide_a_conversation_turn_approval() -> None:
    container = _container()
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    alice = _headers(user="alice", scopes="conversations:read conversations:write runs:write")
    bob = _headers(user="bob", scopes="runs:read runs:write")
    carol = _headers(user="carol", scopes="approvals:read")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        conversation = await client.post(
            "/v1/conversations", headers=alice, json={"agent": "test-agent"}
        )
        assert conversation.status_code == 201, conversation.text
        conversation_id = conversation.json()["id"]
        sent = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=alice,
            json={"text": "update it"},
        )
        assert sent.status_code < 300, sent.text
        approval = await _await_pending(container)
        run_id = approval.run_id

        assert (await client.get("/v1/approvals", headers=bob)).status_code == 403
        for route, body in (
            ("approval", {"decision": "approved"}),
            ("clarification", {"clarification": "yes"}),
        ):
            denied = await client.post(f"/v1/runs/{run_id}/{route}", headers=bob, json=body)
            assert denied.status_code == 404, (route, denied.text)
        stored = await container.runtime.approvals.get(approval.id, "tenant")
        assert stored is not None and stored.status == ApprovalStatus.PENDING

        # An operator granted approvals:read sees it; the owner can still decide it.
        listed = await client.get("/v1/approvals", headers=carol)
        assert [item["id"] for item in listed.json()] == [approval.id]
        decided = await client.post(
            f"/v1/runs/{run_id}/approval", headers=alice, json={"decision": "approved"}
        )
        assert decided.status_code == 202, decided.text
        await container.conversations.shutdown()
        await container.runtime.shutdown(grace_seconds=5)

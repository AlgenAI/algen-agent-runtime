"""Exporting and erasing one user's data across the in-memory stores, the CLI and the API."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from conftest import make_runtime
from user_data_seed import SEEDED_COUNTS, seed_user

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.approvals.service import InMemoryApprovalService
from algen_agent_runtime.cli import main
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.conversations.feedback import InMemoryConversationFeedbackStore
from algen_agent_runtime.conversations.service import (
    ConversationHandlerRegistry,
    ConversationService,
    RuntimeConversationHandler,
)
from algen_agent_runtime.conversations.stores import (
    InMemoryConversationEventBus,
    InMemoryConversationStore,
)
from algen_agent_runtime.events.bus import InMemoryAuditLog, InMemoryEventBus
from algen_agent_runtime.exceptions.errors import ConflictError, UserDataUnsupportedError
from algen_agent_runtime.mcp.tokens import InMemoryMCPTokenStore, MCPOAuthTokenSet, MCPTokenKey
from algen_agent_runtime.memory.store import InMemoryLearnedMemoryStore
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.orchestration.container import Container, build_container
from algen_agent_runtime.persistence import USER_DATA_KINDS, UserDataService
from algen_agent_runtime.persistence.memory import (
    InMemoryArtifactStore,
    InMemoryMemoryStore,
    InMemoryRunStore,
)
from algen_agent_runtime.persistence.tool_executions import InMemoryToolExecutionStore
from algen_agent_runtime.tools.contracts import ToolContext
from algen_agent_runtime.types.contracts import RunRequest, RunStatus, utc_now
from algen_agent_runtime.workflows.store import InMemoryWorkflowCheckpointStore


def stores() -> dict[str, Any]:
    return {
        "runs": InMemoryRunStore(),
        "memory": InMemoryMemoryStore(),
        "events": InMemoryEventBus(),
        "approvals": InMemoryApprovalService(),
        "tool_executions": InMemoryToolExecutionStore(),
        "artifacts": InMemoryArtifactStore(),
        "learned_memory": InMemoryLearnedMemoryStore(),
        "conversations": InMemoryConversationStore(),
        "conversation_events": InMemoryConversationEventBus(),
        "conversation_feedback": InMemoryConversationFeedbackStore(),
        "workflow_checkpoints": InMemoryWorkflowCheckpointStore(),
        "mcp_tokens": InMemoryMCPTokenStore(),
        "audits": InMemoryAuditLog(),
    }


async def connect_mcp(store: Any, tenant: str, user: str) -> None:
    await store.put(
        MCPTokenKey(tenant, user, "calendar"),
        MCPOAuthTokenSet(access_token="token", connected_at=utc_now(), connected_by=user),
    )


async def test_a_dry_run_counts_everything_and_deletes_nothing() -> None:
    seeded = stores()
    await seed_user(seeded, "t", "alice")
    await connect_mcp(seeded["mcp_tokens"], "t", "alice")
    service = UserDataService(**seeded)

    report = await service.erase_user("t", "alice", dry_run=True)

    assert report.dry_run and report.blocked == []
    assert report.counts == {**SEEDED_COUNTS, "mcp_connections": 1}
    assert set(report.counts) == set(USER_DATA_KINDS)
    again = await service.erase_user("t", "alice", dry_run=True)
    assert again.counts == report.counts


async def test_erasure_removes_the_users_data_only_and_is_idempotent() -> None:
    seeded = stores()
    alice = await seed_user(seeded, "t", "alice")
    bob = await seed_user(seeded, "t", "bob")
    other_tenant = await seed_user(seeded, "t2", "alice")
    await connect_mcp(seeded["mcp_tokens"], "t", "alice")
    service = UserDataService(**seeded)

    report = await service.erase_user("t", "alice", actor_id="dpo")

    assert report.counts == {**SEEDED_COUNTS, "mcp_connections": 1}
    for run_id in alice.run_ids:
        assert await seeded["runs"].get(run_id, "t") is None
        assert await seeded["events"].history(run_id) == ()
    for artifact_id in alice.artifact_ids:
        assert await seeded["artifacts"].get(artifact_id, "t") is None
    assert await seeded["memory"].get("t", alice.session_id, 10) == ()
    assert await seeded["memory"].session_owner("t", alice.session_id) is None
    assert await seeded["conversations"].get(alice.conversation_id, "t") is None
    assert await seeded["mcp_tokens"].servers_for_user("t", "alice") == ()
    # Bob's data, and Alice's in another tenant, stay.
    for kept, tenant in ((bob, "t"), (other_tenant, "t2")):
        assert all([await seeded["runs"].get(run_id, tenant) for run_id in kept.run_ids])
        assert await seeded["memory"].get(tenant, kept.session_id, 10)
        assert await seeded["conversations"].get(kept.conversation_id, tenant)
    assert (await service.erase_user("t", "bob", dry_run=True)).counts == SEEDED_COUNTS
    # Again: nothing is left to delete.
    second = await service.erase_user("t", "alice")
    assert second.total == 0
    [audit] = [e for e in await seeded["audits"].list("t") if e.action == "user_data.erased"][:1]
    assert audit.resource_id == "alice" and audit.actor_id == "dpo"
    assert audit.metadata["counts"]["runs"] == 2


async def test_unfinished_work_blocks_erasure_and_a_dry_run_says_why() -> None:
    seeded = stores()
    alice = await seed_user(seeded, "t", "alice", status=RunStatus.AWAITING_APPROVAL)
    service = UserDataService(**seeded)

    dry = await service.erase_user("t", "alice", dry_run=True)
    assert any(alice.run_ids[0] in reason for reason in dry.blocked)
    with pytest.raises(ConflictError, match="nothing was erased"):
        await service.erase_user("t", "alice")
    assert await seeded["runs"].get(alice.run_ids[0], "t") is not None


async def test_an_unresolved_tool_call_blocks_erasure() -> None:
    seeded = stores()
    alice = await seed_user(seeded, "t", "alice")
    context = ToolContext(
        run_id=alice.run_ids[0],
        step_id="step-2",
        tenant_id="t",
        user_id="alice",
        permissions=frozenset(),
        idempotency_key="pending-call",
    )
    await seeded["tool_executions"].begin(context, "send")
    await seeded["tool_executions"].fail("pending-call", "lost", indeterminate=True)
    service = UserDataService(**seeded)

    report = await service.erase_user("t", "alice", dry_run=True)

    assert any("pending-call" in reason and "indeterminate" in reason for reason in report.blocked)
    with pytest.raises(ConflictError):
        await service.erase_user("t", "alice")


async def test_export_is_json_and_holds_every_kind() -> None:
    seeded = stores()
    alice = await seed_user(seeded, "t", "alice")
    await seed_user(seeded, "t", "bob")
    await connect_mcp(seeded["mcp_tokens"], "t", "alice")
    service = UserDataService(**seeded)

    exported = await service.export_user("t", "alice", include_artifact_data=True)

    json.dumps(exported)  # JSON-serialisable as is
    assert [run["id"] for run in exported["runs"]] == alice.run_ids
    assert len(exported["run_events"]) == 2
    assert len(exported["approvals"]) == 2 and len(exported["tool_executions"]) == 2
    assert {item["id"] for item in exported["artifacts"]} == set(alice.artifact_ids)
    assert all(item["data_base64"] for item in exported["artifacts"])
    assert {item["session_id"] for item in exported["session_memory"]} == {
        alice.session_id,
        alice.conversation_id,
    }
    assert exported["learned_memory"][0]["record"]["content"] == "likes tea"
    [conversation] = exported["conversations"]
    assert conversation["conversation"]["id"] == alice.conversation_id
    assert len(conversation["messages"]) == 2 and len(conversation["feedback"]) == 1
    assert exported["workflow_runs"][0]["id"] == alice.workflow_id
    assert exported["mcp_connections"] == [{"server": "calendar"}]
    assert "token" not in json.dumps(exported["mcp_connections"])
    assert "bob" not in json.dumps(exported)


class _CustomRunStore(InMemoryRunStore):
    list_for_user = None  # type: ignore[assignment]


async def test_a_store_without_user_data_support_raises_before_deleting() -> None:
    seeded = stores()
    await seed_user(seeded, "t", "alice")
    seeded["runs"] = _CustomRunStore()
    service = UserDataService(**seeded)

    with pytest.raises(UserDataUnsupportedError, match=r"_CustomRunStore.*list_for_user"):
        await service.erase_user("t", "alice")
    assert (await seeded["memory"].sessions_for_user("t", "alice")) != ()


async def test_runs_through_the_runtime_are_erased_with_their_memory() -> None:
    runtime = make_runtime(MockModelProvider(["first", "second"]))
    for text in ("one", "two"):
        result = await runtime.run(
            RunRequest(agent="test-agent", input=text, tenant_id="t", user_id="u", session_id="s")
        )
        assert result.status == RunStatus.COMPLETED, result.error
    service = UserDataService(
        runs=runtime.runs,
        memory=runtime.memory,
        events=runtime.events,
        approvals=runtime.approvals,
        tool_executions=InMemoryToolExecutionStore(),
        artifacts=InMemoryArtifactStore(),
    )

    report = await service.erase_user("t", "u")

    assert report.counts["runs"] == 2
    assert report.counts["run_events"] > 0
    assert report.counts["session_memory"] == 4 and report.counts["session_owners"] == 1
    assert await runtime.memory.get("t", "s", 10) == ()


def test_the_container_builds_the_service_and_the_cli_erases(
    capsys: pytest.CaptureFixture[str],
) -> None:
    container = build_container(AppSettings())
    assert isinstance(container.user_data, UserDataService)

    with pytest.raises(SystemExit) as exit_info:
        main(["users", "erase", "--tenant", "t", "--user", "u", "--dry-run"])
    assert exit_info.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True and report["counts"]["runs"] == 0

    with pytest.raises(SystemExit) as exit_info:
        main(["users", "export", "--tenant", "t", "--user", "u"])
    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out)["user_id"] == "u"


def _container(seeded: dict[str, Any]) -> Container:
    runtime = make_runtime()
    handlers = ConversationHandlerRegistry()
    handlers.register(RuntimeConversationHandler(runtime))
    return Container(
        runtime=runtime,
        agents=runtime.agents,
        tools=runtime.tools,
        artifacts=seeded["artifacts"],
        events=seeded["events"],
        conversations=ConversationService(
            seeded["conversations"], seeded["conversation_events"], handlers
        ),
        user_data=UserDataService(**seeded),
    )


async def test_admin_routes_need_users_admin_and_stay_in_the_callers_tenant() -> None:
    seeded = stores()
    alice = await seed_user(seeded, "t", "alice")
    await seed_user(seeded, "t2", "alice")
    transport = httpx.ASGITransport(app=create_app(AppSettings(), _container(seeded)))
    admin = {"x-tenant-id": "t", "x-user-id": "dpo", "x-scopes": "users:admin"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        refused = await client.get(
            "/v1/admin/users/alice/data", headers={**admin, "x-scopes": "runs:read"}
        )
        assert refused.status_code == 403
        exported = await client.get("/v1/admin/users/alice/data", headers=admin)
        assert exported.status_code == 200
        assert [run["id"] for run in exported.json()["runs"]] == alice.run_ids
        dry = await client.delete("/v1/admin/users/alice/data?dry_run=true", headers=admin)
        assert dry.json()["counts"]["runs"] == 2
        erased = await client.delete("/v1/admin/users/alice/data", headers=admin)
        assert erased.status_code == 200 and erased.json()["counts"]["runs"] == 2
    # Only tenant t's Alice was erased.
    assert (await seeded["runs"].list_for_user("t2", "alice")) != ()
    assert [e.actor_id for e in await seeded["audits"].list("t") if e.action.startswith("user_")]


async def test_admin_erasure_of_a_busy_user_is_409() -> None:
    seeded = stores()
    await seed_user(seeded, "t", "alice", status=RunStatus.PLANNING)
    transport = httpx.ASGITransport(app=create_app(AppSettings(), _container(seeded)))
    admin = {"x-tenant-id": "t", "x-user-id": "dpo", "x-scopes": "users:admin"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete("/v1/admin/users/alice/data", headers=admin)
    assert response.status_code == 409

"""Only approved, rejected and modified decide an approval request.

Before 0.2.0b4 a decision of ``pending`` or ``expired`` reached ``Runtime.resume`` as a valid
``ApprovalStatus`` and fell through to approving the tool call, which then ran.
"""

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
    check_decision,
)
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolDefinition
from algen_agent_runtime.types.contracts import RunRequest, RunState, RunStatus, utc_now


def _send_definition() -> ToolDefinition:
    return ToolDefinition(
        name="mail.send",
        version="1",
        description="Send",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect=SideEffect.EXTERNAL,
        requires_approval=True,
    )


async def _awaiting_approval(seen: list[Any]) -> tuple[AgentRuntime, RunState]:
    provider = MockModelProvider([tool_call_response("mail.send", {"to": "bob"}), "done"])
    runtime = make_runtime(provider, make_agent(enabled_tools=frozenset({"mail.send"})))
    runtime.tools.register(Tool(_send_definition(), lambda args, ctx: seen.append(args) or {}))
    state = await runtime.start(
        RunRequest(agent="test-agent", input="x", tenant_id="tenant", user_id="user")
    )
    for _ in range(400):
        state = await runtime.status(state.id, "tenant")
        if state.status == RunStatus.AWAITING_APPROVAL:
            return runtime, state
        await asyncio.sleep(0.005)
    raise AssertionError(f"run did not pause for approval: {state.status}")


@pytest.mark.parametrize("decision", ["pending", "expired", "", "APPROVED"])
async def test_a_non_decision_never_runs_the_tool(decision: str) -> None:
    seen: list[Any] = []
    runtime, state = await _awaiting_approval(seen)
    approval_id = state.pause_payload["approval_id"]  # type: ignore[index]
    try:
        with pytest.raises(ValueError, match="approved, rejected, or modified"):
            await runtime.resume(state.id, "tenant", {"decision": decision}, decided_by="user")
        await asyncio.sleep(0.02)
        assert seen == []
        assert (await runtime.status(state.id, "tenant")).status == RunStatus.AWAITING_APPROVAL
        approval = await runtime.approvals.get(approval_id, "tenant")
        assert approval is not None and approval.status == ApprovalStatus.PENDING
        assert approval.decided_by is None
    finally:
        await runtime.shutdown(grace_seconds=5)


# Every approval status, sorted by what deciding with it does. A status added later fails
# test_every_approval_status_is_sorted until someone decides whether it runs the tool; only an
# explicit approve may (AGENTS.md, "Security invariants").
RUNS_THE_TOOL = frozenset({ApprovalStatus.APPROVED, ApprovalStatus.MODIFIED})
SKIPS_THE_TOOL = frozenset({ApprovalStatus.REJECTED})
REFUSED = frozenset({ApprovalStatus.PENDING, ApprovalStatus.EXPIRED})


def test_every_approval_status_is_sorted() -> None:
    assert RUNS_THE_TOOL | SKIPS_THE_TOOL | REFUSED == set(ApprovalStatus)
    assert len(RUNS_THE_TOOL) + len(SKIPS_THE_TOOL) + len(REFUSED) == len(ApprovalStatus)


@pytest.mark.parametrize("status", list(ApprovalStatus))
async def test_only_an_approving_decision_runs_the_tool(status: ApprovalStatus) -> None:
    seen: list[Any] = []
    runtime, state = await _awaiting_approval(seen)
    response: dict[str, Any] = {"decision": status.value}
    if status is ApprovalStatus.MODIFIED:
        response["modified_parameters"] = {"to": "eve"}
    try:
        if status in REFUSED:
            with pytest.raises(ValueError):
                await runtime.resume(state.id, "tenant", response, decided_by="user")
            await asyncio.sleep(0.02)
            assert (await runtime.status(state.id, "tenant")).status == RunStatus.AWAITING_APPROVAL
        else:
            await runtime.resume(state.id, "tenant", response, decided_by="user")
            await runtime.wait(state.id, "tenant")
        assert bool(seen) == (status in RUNS_THE_TOOL), seen
    finally:
        await runtime.shutdown(grace_seconds=5)


async def test_approved_with_modified_parameters_is_refused_by_the_runtime() -> None:
    seen: list[Any] = []
    runtime, state = await _awaiting_approval(seen)
    try:
        with pytest.raises(ValueError, match="only allowed with a modified decision"):
            await runtime.resume(
                state.id,
                "tenant",
                {"decision": "approved", "modified_parameters": {"to": "eve"}},
                decided_by="user",
            )
        assert seen == []
    finally:
        await runtime.shutdown(grace_seconds=5)


async def test_an_approved_decision_still_runs_the_tool() -> None:
    seen: list[Any] = []
    runtime, state = await _awaiting_approval(seen)
    try:
        await runtime.resume(state.id, "tenant", {"decision": "approved"}, decided_by="user")
        done = await runtime.wait(state.id, "tenant")
        assert done.status == RunStatus.COMPLETED
        assert seen == [{"to": "bob"}]
    finally:
        await runtime.shutdown(grace_seconds=5)


@pytest.mark.parametrize(
    ("decision", "parameters", "message"),
    [
        (ApprovalStatus.PENDING, None, "approved, rejected, or modified"),
        (ApprovalStatus.EXPIRED, None, "approved, rejected, or modified"),
        ("bogus", None, "approved, rejected, or modified"),
        (ApprovalStatus.APPROVED, {"to": "eve"}, "only allowed with a modified"),
        (ApprovalStatus.REJECTED, {"to": "eve"}, "only allowed with a modified"),
        (ApprovalStatus.MODIFIED, None, "requires modified_parameters"),
    ],
)
def test_check_decision_refuses(decision: Any, parameters: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_decision(decision, parameters)


def _request() -> ApprovalRequest:
    return ApprovalRequest(
        run_id="run-1",
        step_id="step-1",
        tenant_id="tenant-1",
        proposed_action="Send",
        side_effect_summary="Send mail",
        redacted_parameters={},
        risk="external",
        expires_at=utc_now() + timedelta(minutes=5),
    )


class _ApprovalDatabase:
    def __init__(self, approval: ApprovalRequest) -> None:
        self.approval_json = approval.model_dump_json(exclude={"agent", "created_at"})
        self.writes = 0

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        return {"approval": self.approval_json, "agent": None, "created_at": utc_now()}

    async def execute(self, query: str, *args: Any) -> str:
        self.writes += 1
        self.approval_json = args[0]
        return "UPDATE 1"


@pytest.mark.parametrize("decision", [ApprovalStatus.PENDING, ApprovalStatus.EXPIRED])
async def test_in_memory_service_refuses_a_non_decision(decision: ApprovalStatus) -> None:
    service = InMemoryApprovalService()
    request = await service.create("run-1", "s1", "t", "Send", {}, "external", 600)
    with pytest.raises(ValueError):
        await service.decide(request.id, "t", decision)
    with pytest.raises(ValueError):
        await service.decide(request.id, "t", ApprovalStatus.APPROVED, {"to": "eve"})
    stored = await service.get(request.id, "t")
    assert stored is not None
    assert stored.status == ApprovalStatus.PENDING
    assert stored.modified_parameters is None


@pytest.mark.parametrize("decision", [ApprovalStatus.PENDING, ApprovalStatus.EXPIRED])
async def test_postgres_service_refuses_a_non_decision(decision: ApprovalStatus) -> None:
    database = _ApprovalDatabase(_request())
    service = PostgresApprovalService(database)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await service.decide(_request().id, "tenant-1", decision)
    with pytest.raises(ValueError):
        await service.decide(_request().id, "tenant-1", ApprovalStatus.APPROVED, {"to": "eve"})
    assert database.writes == 0


@pytest.mark.parametrize(
    "body",
    [
        {"decision": "pending"},
        {"decision": "expired"},
        {"decision": "approved", "modified_parameters": {"to": "eve"}},
        {"decision": "modified"},
    ],
)
async def test_http_approval_refuses_a_non_decision(body: dict[str, Any]) -> None:
    transport = httpx.ASGITransport(app=create_app(AppSettings()))
    headers = {"x-tenant-id": "t", "x-user-id": "u", "x-scopes": "runs:write"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/runs/run-1/approval", json=body, headers=headers)
    assert response.status_code == 422

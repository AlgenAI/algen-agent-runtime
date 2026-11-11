"""A run's approval records who decided it, when, and their comment (RT-G01)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import make_agent, make_runtime
from structlog.testing import capture_logs

from algen_agent_runtime.approvals.service import (
    STORED_JSON_EXCLUDE,
    ApprovalRequest,
    ApprovalStatus,
    InMemoryApprovalService,
    PostgresApprovalService,
)
from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.types.contracts import RunRequest, RunState, RunStatus, utc_now

MIGRATIONS = Path(__file__).parents[2] / "src/algen_agent_runtime/persistence/migrations"
FIELDS = ("decided_by", "decided_at", "comment")


async def _created(service: Any, run_id: str = "run-1") -> ApprovalRequest:
    request: ApprovalRequest = await service.create(
        run_id, "s1", "tenant", "Execute mail.send", {}, "external", 600
    )
    return request


# The in-memory service


async def test_a_decision_records_the_approver_the_time_and_the_comment() -> None:
    service = InMemoryApprovalService()
    created = await _created(service)
    assert (created.decided_by, created.decided_at, created.comment) == (None, None, None)
    before = utc_now()

    decided = await service.decide(
        created.id, "tenant", ApprovalStatus.APPROVED, decided_by="carol", comment="Looks right."
    )

    assert (decided.decided_by, decided.comment) == ("carol", "Looks right.")
    assert decided.decided_at is not None and decided.decided_at >= before
    read = await service.get(created.id, "tenant")
    assert read is not None and (read.decided_by, read.decided_at) == ("carol", decided.decided_at)
    [listed] = await service.list("tenant")
    assert listed.decided_by == "carol"
    # An expiry after the decision (the tool changed) keeps who approved it.
    expired = await service.expire(created.id, "tenant", reason="tool_changed")
    assert (expired.status, expired.decided_by, expired.comment) == (
        ApprovalStatus.EXPIRED,
        "carol",
        "Looks right.",
    )


async def test_a_decision_without_an_approver_still_records_its_time() -> None:
    service = InMemoryApprovalService()
    created = await _created(service)

    decided = await service.decide(created.id, "tenant", ApprovalStatus.REJECTED)

    assert (decided.status, decided.decided_by, decided.comment) == (
        ApprovalStatus.REJECTED,
        None,
        None,
    )
    assert decided.decided_at is not None


async def test_a_late_or_refused_decision_records_nothing() -> None:
    service = InMemoryApprovalService()
    late = await service.create("run-1", "s1", "tenant", "x", {}, "write", -1)
    with pytest.raises(ConflictError, match="expired"):
        await service.decide(late.id, "tenant", ApprovalStatus.APPROVED, decided_by="carol")
    read = await service.get(late.id, "tenant")
    assert read is not None and read.status == ApprovalStatus.EXPIRED
    assert (read.decided_by, read.decided_at) == (None, None)

    created = await _created(service)
    await service.decide(created.id, "tenant", ApprovalStatus.APPROVED, decided_by="carol")
    with pytest.raises(ConflictError, match="already been decided"):
        await service.decide(created.id, "tenant", ApprovalStatus.REJECTED, decided_by="dave")
    read = await service.get(created.id, "tenant")
    assert read is not None and read.decided_by == "carol"


@pytest.mark.parametrize(
    ("decided_by", "comment"), [("", None), ("x" * 257, None), ("carol", "x" * 4001)]
)
async def test_an_invalid_approver_or_comment_is_refused_before_deciding(
    decided_by: str, comment: str | None
) -> None:
    service = InMemoryApprovalService()
    created = await _created(service)

    with pytest.raises(ValueError):
        await service.decide(
            created.id, "tenant", ApprovalStatus.APPROVED, decided_by=decided_by, comment=comment
        )
    read = await service.get(created.id, "tenant")
    assert read is not None and read.status == ApprovalStatus.PENDING


# The PostgreSQL service, against the rows asyncpg returns


class _Table:
    """The approvals table as asyncpg returns its rows: the JSON as text, and the columns."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if query.startswith("INSERT"):
            columns = query.split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
            row = dict(zip(columns, args, strict=True))
            row.update(created_at=None, status_reason=None, **dict.fromkeys(FIELDS))
            self.rows[args[0]] = row
            return {"created_at": None}
        return self.rows.get(args[0])

    async def execute(self, query: str, *args: Any) -> str:
        assert "decided_by=$6, decided_at=$7, comment=$8" in query
        row = self.rows[args[1]]
        row["approval"], row["status_reason"] = args[0], args[4]
        row["decided_by"], row["decided_at"], row["comment"] = args[5:8]
        return "UPDATE 1"


async def test_postgres_keeps_the_decision_in_columns_not_the_json() -> None:
    table = _Table()
    service = PostgresApprovalService(table)
    created = await _created(service)

    decided = await service.decide(
        created.id, "tenant", ApprovalStatus.MODIFIED, {"to": "ann"}, decided_by="carol"
    )

    row = table.rows[created.id]
    assert (row["decided_by"], row["decided_at"], row["comment"]) == (
        "carol",
        decided.decided_at,
        None,
    )
    assert not set(FIELDS) & set(json.loads(row["approval"]))
    assert set(FIELDS) <= STORED_JSON_EXCLUDE
    read = await service.get(created.id, "tenant")
    assert read is not None
    assert (read.status, read.decided_by, read.decided_at) == (
        ApprovalStatus.MODIFIED,
        "carol",
        decided.decided_at,
    )


async def test_postgres_reads_a_row_without_the_020_columns() -> None:
    table = _Table()
    service = PostgresApprovalService(table)
    created = await _created(service)
    for field in FIELDS:
        del table.rows[created.id][field]

    read = await service.get(created.id, "tenant")

    assert read is not None and (read.decided_by, read.decided_at, read.comment) == (
        None,
        None,
        None,
    )


def test_migration_020_adds_nullable_columns_and_is_forward_only() -> None:
    migration = (MIGRATIONS / "020_approval_decided_by.sql").read_text(encoding="utf-8")
    for column, kind in (
        ("decided_by", "text"),
        ("decided_at", "timestamptz"),
        ("comment", "text"),
    ):
        assert (
            f"ALTER TABLE algen_agent_runtime_approvals ADD COLUMN IF NOT EXISTS {column} {kind};"
            in migration
        )
    assert "NOT NULL" not in migration and "DROP" not in migration.upper()


# Runtime.resume


def _runtime() -> AgentRuntime:
    provider = MockModelProvider([tool_call_response("mail.send", {"to": "bob"}), "done"] * 2)
    runtime = make_runtime(provider, make_agent(enabled_tools=frozenset({"mail.send"})))

    def send(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        return {"sent": True}

    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="mail.send",
                version="1",
                description="Send an email",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                side_effect=SideEffect.EXTERNAL,
                requires_approval=True,
            ),
            send,
        )
    )
    return runtime


async def _paused(runtime: AgentRuntime) -> RunState:
    state = await runtime.start(
        RunRequest(agent="test-agent", input="send it", tenant_id="tenant", user_id="user")
    )
    for _ in range(400):
        state = await runtime.status(state.id, "tenant")
        if state.status == RunStatus.AWAITING_APPROVAL:
            return state
        await asyncio.sleep(0.005)
    raise AssertionError(f"run is {state.status}, not awaiting approval")


async def test_resume_records_the_approver_and_the_payloads_comment() -> None:
    runtime = _runtime()
    paused = await _paused(runtime)
    approval_id = str((paused.pause_payload or {})["approval_id"])

    await runtime.resume(
        paused.id,
        "tenant",
        {"decision": "approved", "comment": " Sent with the invoice. "},
        decided_by="carol",
    )

    approval = await runtime.approvals.get(approval_id, "tenant")
    assert approval is not None
    assert (approval.status, approval.decided_by, approval.comment) == (
        ApprovalStatus.APPROVED,
        "carol",
        "Sent with the invoice.",
    )
    assert (await runtime.wait(paused.id, "tenant")).status == RunStatus.COMPLETED
    await runtime.shutdown(grace_seconds=5)


async def test_resume_records_a_rejections_reason_as_the_comment() -> None:
    runtime = _runtime()
    paused = await _paused(runtime)
    approval_id = str((paused.pause_payload or {})["approval_id"])

    await runtime.resume(paused.id, "tenant", {"decision": "rejected", "reason": "Not this week."})

    approval = await runtime.approvals.get(approval_id, "tenant")
    assert approval is not None
    assert (approval.status, approval.decided_by, approval.comment) == (
        ApprovalStatus.REJECTED,
        None,
        "Not this week.",
    )
    await runtime.shutdown(grace_seconds=5)


class _LegacyApprovals(InMemoryApprovalService):
    """A custom service written against the 0.1 protocol: decide has no decided_by or comment."""

    async def decide(  # type: ignore[override]
        self,
        approval_id: str,
        tenant_id: str,
        decision: ApprovalStatus,
        modified_parameters: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        return await super().decide(approval_id, tenant_id, decision, modified_parameters)


class _KeywordApprovals(InMemoryApprovalService):
    """A custom service that takes any keyword and forwards it."""

    def __init__(self) -> None:
        super().__init__()
        self.keywords: list[dict[str, Any]] = []

    async def decide(  # type: ignore[override]
        self,
        approval_id: str,
        tenant_id: str,
        decision: ApprovalStatus,
        modified_parameters: dict[str, Any] | None = None,
        **keywords: Any,
    ) -> ApprovalRequest:
        self.keywords.append(keywords)
        return await super().decide(
            approval_id, tenant_id, decision, modified_parameters, **keywords
        )


async def test_a_service_without_the_new_keywords_keeps_working_and_is_warned_once() -> None:
    runtime = _runtime()
    runtime.approvals = _LegacyApprovals()
    with capture_logs() as logs:
        for _ in range(2):
            paused = await _paused(runtime)
            approval_id = str((paused.pause_payload or {})["approval_id"])
            await runtime.resume(paused.id, "tenant", {"decision": "approved"}, decided_by="carol")
            assert (await runtime.wait(paused.id, "tenant")).status == RunStatus.COMPLETED
            approval = await runtime.approvals.get(approval_id, "tenant")
            assert approval is not None and approval.status == ApprovalStatus.APPROVED
            assert approval.decided_by is None

    warnings = [log for log in logs if log["event"] == "approval_service_records_no_approver"]
    assert len(warnings) == 1
    assert warnings[0]["approval_service"] == "_LegacyApprovals"
    assert warnings[0]["missing"] == ["comment", "decided_by"]
    await runtime.shutdown(grace_seconds=5)


async def test_a_service_taking_any_keyword_receives_the_approver() -> None:
    runtime = _runtime()
    approvals = _KeywordApprovals()
    runtime.approvals = approvals
    paused = await _paused(runtime)

    await runtime.resume(paused.id, "tenant", {"decision": "approved"}, decided_by="carol")

    assert approvals.keywords == [{"decided_by": "carol", "comment": None}]
    await runtime.shutdown(grace_seconds=5)

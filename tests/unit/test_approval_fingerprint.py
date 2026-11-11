"""The tool is fingerprinted when approval is asked for, and asked again if it changed (#127)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.approvals.service import (
    STORED_JSON_EXCLUDE,
    ApprovalStatus,
    InMemoryApprovalService,
    PostgresApprovalService,
)
from algen_agent_runtime.exceptions.errors import ConflictError, NotFoundError
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.adapters import mcp_tool
from algen_agent_runtime.tools.authoring import tool, write
from algen_agent_runtime.tools.contracts import (
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
    canonical_digest,
    tool_fingerprint,
)
from algen_agent_runtime.types.contracts import RunRequest, RunState, RunStatus
from algen_agent_runtime.workflows import MultiAgentWorkflowExecutor, WorkflowHookRegistry
from algen_agent_runtime.workflows.contracts import (
    WorkflowApprovalRule,
    WorkflowManifest,
    WorkflowNode,
    WorkflowNodeKind,
    WorkflowStatus,
)

MIGRATIONS = Path(__file__).parents[2] / "src/algen_agent_runtime/persistence/migrations"


def _definition(**updates: Any) -> ToolDefinition:
    values: dict[str, Any] = {
        "name": "mail.send",
        "version": "1",
        "description": "Send an email",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "side_effect": SideEffect.EXTERNAL,
        "requires_approval": True,
    }
    values.update(updates)
    return ToolDefinition(**values)


def _send_v1(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return {"sent": 1}


def _send_v2(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return {"sent": 2}


# The fingerprint


def test_the_fingerprint_covers_the_definition_and_the_handler_source() -> None:
    base = tool_fingerprint(Tool(_definition(), _send_v1))
    assert base == tool_fingerprint(Tool(_definition(), _send_v1))
    assert len(base) == 64
    for changed in (
        Tool(_definition(version="2"), _send_v1),
        Tool(_definition(input_schema={"type": "object", "required": ["to"]}), _send_v1),
        Tool(_definition(output_schema={"type": "array"}), _send_v1),
        Tool(_definition(side_effect=SideEffect.DESTRUCTIVE), _send_v1),
        Tool(_definition(requires_approval=False), _send_v1),
        Tool(_definition(), _send_v2),
    ):
        assert tool_fingerprint(changed) != base
    # The description, a timeout or a permission do not change what runs.
    assert (
        tool_fingerprint(Tool(_definition(description="Other", timeout_seconds=5), _send_v1))
        == base
    )


def test_a_tool_that_runs_elsewhere_or_has_no_source_is_fingerprinted_by_definition() -> None:
    definition_only = tool_fingerprint(Tool(_definition(), _send_v1, source=None))
    assert definition_only != tool_fingerprint(Tool(_definition(), _send_v1))
    assert tool_fingerprint(Tool(_definition(), lambda a, c: {})) == definition_only
    assert tool_fingerprint(Tool(_definition(), print)) == definition_only  # C-backed

    class Client:
        async def call_tool(self, name: str, arguments: Any) -> Any:
            return {}

    assert tool_fingerprint(mcp_tool(_definition(), "send", Client())) == definition_only


def test_a_typed_tool_is_fingerprinted_by_the_authors_function() -> None:
    @tool(write(approval=True), name="mail.send")
    def send(to: str) -> dict[str, str]:
        """Send an email."""
        return {"to": to}

    assert send.source is not None and send.source.__name__ == "send"
    unwrapped = Tool(send.definition, send.handler, source=None)
    assert tool_fingerprint(send) != tool_fingerprint(unwrapped)
    # Wrapping the handler (middleware) keeps the source.
    assert tool_fingerprint(send.with_handler(lambda a, c: {})) == tool_fingerprint(send)


# The approval record


async def test_the_approval_records_what_it_covers_and_can_be_expired_with_a_reason() -> None:
    service = InMemoryApprovalService()
    fingerprint = "a" * 64
    created = await service.create(
        "run-1",
        "s1",
        "tenant",
        "Execute mail.send",
        {"to": "bob"},
        "external",
        600,
        tool="mail.send",
        tool_version="1",
        tool_fingerprint=fingerprint,
        arguments_sha256=canonical_digest({"to": "bob"}),
    )
    assert (created.tool, created.tool_version, created.tool_fingerprint) == (
        "mail.send",
        "1",
        fingerprint,
    )
    await service.decide(created.id, "tenant", ApprovalStatus.APPROVED)

    expired = await service.expire(created.id, "tenant", reason="tool_changed")

    assert (expired.status, expired.status_reason) == (ApprovalStatus.EXPIRED, "tool_changed")
    with pytest.raises(ConflictError):
        await service.expire(created.id, "tenant", reason="tool_changed")
    with pytest.raises(NotFoundError):
        await service.expire(created.id, "other", reason="tool_changed")
    rejected = await service.create("run-1", "s2", "tenant", "x", {}, "write", 600)
    await service.decide(rejected.id, "tenant", ApprovalStatus.REJECTED)
    with pytest.raises(ConflictError):
        await service.expire(rejected.id, "tenant", reason="tool_changed")


class _Table:
    """The approvals table as asyncpg returns its rows: the JSON as text, and the columns."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.updates: list[tuple[Any, ...]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if query.startswith("INSERT"):
            columns = query.split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
            row = dict(zip(columns, args, strict=True))
            row["created_at"] = None
            row["status_reason"] = None
            self.rows[args[0]] = row
            return {"created_at": None}
        return self.rows.get(args[0])

    async def execute(self, query: str, *args: Any) -> str:
        self.updates.append(args)
        row = self.rows[args[1]]
        row["approval"], row["status_reason"] = args[0], args[4]
        return "UPDATE 1"


async def test_postgres_keeps_the_new_fields_in_columns_not_the_json() -> None:
    table = _Table()
    service = PostgresApprovalService(table)
    created = await service.create(
        "run-1",
        "s1",
        "tenant",
        "Execute mail.send",
        {},
        "external",
        600,
        agent="a",
        tool="mail.send",
        tool_version="1",
        tool_fingerprint="b" * 64,
        arguments_sha256="c" * 64,
    )
    row = table.rows[created.id]
    assert (row["tool"], row["tool_version"], row["tool_fingerprint"], row["arguments_sha256"]) == (
        "mail.send",
        "1",
        "b" * 64,
        "c" * 64,
    )
    assert "tool_fingerprint" not in row["approval"] and "status_reason" not in row["approval"]
    await service.decide(created.id, "tenant", ApprovalStatus.APPROVED)
    expired = await service.expire(created.id, "tenant", reason="tool_changed")
    assert expired.status_reason == "tool_changed"
    read = await service.get(created.id, "tenant")
    assert read is not None
    assert (read.status, read.status_reason, read.tool_fingerprint) == (
        ApprovalStatus.EXPIRED,
        "tool_changed",
        "b" * 64,
    )
    assert {"tool", "tool_version", "tool_fingerprint", "arguments_sha256", "status_reason"} <= (
        STORED_JSON_EXCLUDE
    )


def test_migration_017_adds_nullable_columns_and_is_forward_only() -> None:
    migration = (MIGRATIONS / "017_approval_tool_fingerprint.sql").read_text(encoding="utf-8")
    for column in ("tool", "tool_version", "tool_fingerprint", "arguments_sha256", "status_reason"):
        assert (
            f"ALTER TABLE algen_agent_runtime_approvals ADD COLUMN IF NOT EXISTS {column} text;"
            in migration
        )
    assert "NOT NULL" not in migration and "DROP" not in migration.upper()


# Agent runs


def _runtime(seen: list[str]) -> AgentRuntime:
    provider = MockModelProvider([tool_call_response("mail.send", {"to": "bob"}), "done"])
    runtime = make_runtime(provider, make_agent(enabled_tools=frozenset({"mail.send"})))

    def send(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        seen.append("v1")
        return {"sent": True}

    runtime.tools.register(Tool(_definition(), send))
    return runtime


def _deploy(runtime: AgentRuntime, seen: list[str], version: str = "1") -> Tool:
    """A deploy lands while the run is paused: same name, other code."""

    def send(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        seen.append("v2")
        return {"sent": True}

    new = Tool(_definition(version=version), send)
    runtime.tools._tools["mail.send"] = new
    return new


async def _wait_for(runtime: AgentRuntime, run_id: str, status: RunStatus) -> RunState:
    for _ in range(400):
        state = await runtime.status(run_id, "tenant")
        if state.status == status:
            return state
        await asyncio.sleep(0.005)
    raise AssertionError(f"run is {state.status}, not {status}: {state.error}")


async def _paused(runtime: AgentRuntime) -> RunState:
    state = await runtime.start(
        RunRequest(agent="test-agent", input="send it", tenant_id="tenant", user_id="user")
    )
    return await _wait_for(runtime, state.id, RunStatus.AWAITING_APPROVAL)


async def test_the_pause_and_the_approval_record_the_tool_fingerprint() -> None:
    seen: list[str] = []
    runtime = _runtime(seen)
    paused = await _paused(runtime)
    approval = await runtime.approvals.get(paused.pause_payload["approval_id"], "tenant")

    expected = tool_fingerprint(runtime.tools.get("mail.send"))
    assert approval is not None
    assert (approval.tool, approval.tool_version, approval.tool_fingerprint) == (
        "mail.send",
        "1",
        expected,
    )
    assert approval.arguments_sha256 == canonical_digest({"to": "bob"})
    assert paused.pause_payload["tool_fingerprint"] == expected
    assert paused.pause_payload["tool"] == "mail.send"
    await runtime.shutdown(grace_seconds=5)


@pytest.mark.parametrize("version", ["1", "2"])
async def test_a_tool_changed_before_the_approved_call_runs_is_asked_for_again(
    version: str,
) -> None:
    seen: list[str] = []
    runtime = _runtime(seen)
    paused = await _paused(runtime)
    first = paused.pause_payload["approval_id"]
    new = _deploy(runtime, seen, version)

    await runtime.resume(paused.id, "tenant", {"decision": "approved"})
    again = await _wait_for(runtime, paused.id, RunStatus.AWAITING_APPROVAL)
    second = again.pause_payload["approval_id"]

    assert second != first and seen == []
    assert again.pause_payload["replaces_approval_id"] == first
    assert again.pause_payload["replaced_reason"] == "tool_changed"
    assert again.pause_payload["tool_fingerprint"] == tool_fingerprint(new)
    expired = await runtime.approvals.get(first, "tenant")
    assert expired is not None
    assert (expired.status, expired.status_reason) == (ApprovalStatus.EXPIRED, "tool_changed")
    [audit] = [
        event
        for event in await runtime.audits.list("tenant")
        if event.action == "approval.tool_changed"
    ]
    assert audit.resource_id == first
    assert audit.metadata["current_fingerprint"] == tool_fingerprint(new)
    assert audit.metadata["previous_fingerprint"] != audit.metadata["current_fingerprint"]

    await runtime.resume(paused.id, "tenant", {"decision": "approved"})
    done = await runtime.wait(paused.id, "tenant")
    assert done.status == RunStatus.COMPLETED, done.error
    assert seen == ["v2"]
    await runtime.shutdown(grace_seconds=5)


async def test_an_approval_recorded_without_a_fingerprint_runs_as_before() -> None:
    seen: list[str] = []
    runtime = _runtime(seen)
    paused = await _paused(runtime)
    approvals = runtime.approvals
    assert isinstance(approvals, InMemoryApprovalService)
    stored = approvals._items[paused.pause_payload["approval_id"]]
    # As stored before 0.2: no tool, version, fingerprint or digest.
    for field in ("tool", "tool_version", "tool_fingerprint", "arguments_sha256"):
        setattr(stored, field, None)
    _deploy(runtime, seen)

    await runtime.resume(paused.id, "tenant", {"decision": "approved"})
    done = await runtime.wait(paused.id, "tenant")

    assert done.status == RunStatus.COMPLETED, done.error
    assert seen == ["v2"]
    await runtime.shutdown(grace_seconds=5)


# Workflow tool steps


def _workflow_runtime(seen: list[str]) -> Any:
    runtime = make_runtime()

    def send(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        seen.append("v1")
        return {"sent": True}

    runtime.tools.register(Tool(_definition(), send))
    return runtime


def _manifest() -> WorkflowManifest:
    return WorkflowManifest(
        name="mail",
        version="1.0.0",
        output_key="sent",
        nodes=(
            WorkflowNode(
                id="review",
                kind=WorkflowNodeKind.APPROVAL,
                output_key="review",
                approval=WorkflowApprovalRule(prompt="Send?"),
            ),
            WorkflowNode(
                id="send",
                kind=WorkflowNodeKind.HANDLER,
                handler="tool:mail.send",
                depends_on=("review",),
                arguments={"to": "bob"},
                output_key="sent",
            ),
        ),
    )


async def test_a_workflow_step_whose_tool_changed_is_asked_for_again() -> None:
    seen: list[str] = []
    runtime = _workflow_runtime(seen)
    executor = MultiAgentWorkflowExecutor(runtime, WorkflowHookRegistry())
    manifest = _manifest()
    events: list[tuple[str, dict[str, Any]]] = []
    paused = await executor.run(manifest, {}, tenant_id="tenant", user_id="user")
    before = paused.pause["calls"][0]["tool_fingerprint"]
    assert before == tool_fingerprint(runtime.tools.get("mail.send"))
    new = _deploy(runtime, seen)

    again = await executor.decide_approval(
        manifest,
        paused.id,
        tenant_id="tenant",
        user_id="approver",
        decision="approved",
        emit=lambda kind, data: events.append((kind, data)),
    )

    assert again.status == WorkflowStatus.AWAITING_APPROVAL, again.error
    assert seen == []
    assert again.pause["calls"][0]["tool_fingerprint"] == tool_fingerprint(new)
    expired = next(data for kind, data in events if kind == "workflow.approval.expired")
    assert expired["reason"] == "tool_changed"
    changed = next(data for kind, data in events if kind == "workflow.approval.tool_changed")
    assert (changed["previous_fingerprint"], changed["current_fingerprint"]) == (
        before,
        tool_fingerprint(new),
    )
    assert changed["tool"] == "mail.send" and changed["tool_node_id"] == "send"

    done = await executor.decide_approval(
        manifest, paused.id, tenant_id="tenant", user_id="approver", decision="approved"
    )
    assert done.status == WorkflowStatus.COMPLETED, done.error
    assert seen == ["v2"]


async def test_a_workflow_approval_asked_before_0_2_is_not_checked_against_the_tool() -> None:
    seen: list[str] = []
    runtime = _workflow_runtime(seen)
    executor = MultiAgentWorkflowExecutor(runtime, WorkflowHookRegistry())
    manifest = _manifest()
    paused = await executor.run(manifest, {}, tenant_id="tenant", user_id="user")
    # A pause written before 0.2 shows no calls, so it recorded no fingerprint.
    stored = await executor._require_state(paused.id, "tenant")
    assert stored.pause is not None
    stored.pause.pop("calls")
    await executor._checkpoint(stored)
    _deploy(runtime, seen)

    done = await executor.decide_approval(
        manifest, paused.id, tenant_id="tenant", user_id="approver", decision="approved"
    )

    assert done.status == WorkflowStatus.COMPLETED, done.error
    assert seen == ["v2"]

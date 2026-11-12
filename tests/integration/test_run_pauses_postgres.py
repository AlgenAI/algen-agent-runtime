"""Run pauses that survive a restart on live PostgreSQL stores: a child run's approval shown on
its parent, core.ask_user, a recovered run waiting for credentials, and event subscribers that
fall behind (0.2.0b3)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any
from uuid import uuid4

import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.approvals.service import ApprovalStatus, PostgresApprovalService
from algen_agent_runtime.events.bus import PostgresEventBus
from algen_agent_runtime.events.contracts import RunEvent
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.persistence.postgres import (
    PostgresDatabase,
    PostgresRunStore,
    PostgresToolExecutionStore,
)
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.subagent import subagent_tool
from algen_agent_runtime.types.contracts import (
    ModelResponse,
    Role,
    RunRequest,
    RunState,
    RunStatus,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("POSTGRES_DSN"), reason="run pauses require a live PostgreSQL"
    ),
]

SEND = tool_call_response("mail.send", {"to": "bob"})
DELEGATION = tool_call_response("core.subagent", {"agent": "child-agent", "input": "mail bob"})


@pytest.fixture
async def database() -> AsyncIterator[PostgresDatabase]:
    pytest.importorskip("asyncpg")
    db = PostgresDatabase(os.environ["POSTGRES_DSN"])
    await db.start()
    try:
        yield db
    finally:
        await db.close()


class _TenantRuns(PostgresRunStore):
    """The database's runs, of which recovery sees only one tenant's: the database is shared with
    other tests, whose active runs this process must not drive."""

    def __init__(self, db: PostgresDatabase, tenant: str) -> None:
        super().__init__(db)
        self._tenant = tenant

    async def list_active(self, limit: int = 1000) -> Sequence[RunState]:
        return [
            state
            for state in await super().list_active(100_000)
            if state.request.tenant_id == self._tenant
        ][:limit]


def _runtime(
    db: PostgresDatabase,
    *responses: Any,
    sent: list[str] | None = None,
    tenant: str = "",
) -> AgentRuntime:
    """A runtime process over the database's stores; build another to 'restart'."""
    runtime = make_runtime(
        MockModelProvider(list(responses)),
        make_agent(
            enabled_tools=frozenset({"core.subagent", "core.ask_user", "api.read"}),
            tool_permissions=frozenset({"agents.delegate", "user.ask"}),
            delegates=frozenset({"child-agent"}),
        ),
    )
    runtime.agents.register(make_agent(name="child-agent", enabled_tools=frozenset({"mail.send"})))
    runtime.runs = _TenantRuns(db, tenant)
    runtime.events = PostgresEventBus(db)
    runtime.approvals = PostgresApprovalService(db)
    runtime.tool_executor = ToolExecutor(
        runtime.tools, runtime.policies, PostgresToolExecutionStore(db)
    )
    log = sent if sent is not None else []

    def send(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        log.append(str(arguments.get("to")))
        return {"sent": True}

    def read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        return {"signed_in": bool(context.credentials)}

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
    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="api.read",
                version="1",
                description="Read as the user",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                side_effect=SideEffect.READ,
            ),
            read,
        )
    )
    runtime.tools.register(subagent_tool(runtime))
    return runtime


def _request(tenant: str) -> RunRequest:
    return RunRequest(agent="test-agent", input="go", tenant_id=tenant, user_id="alice")


async def test_a_childs_approval_on_the_parent_survives_a_restart(
    database: PostgresDatabase,
) -> None:
    tenant = f"tenant-{uuid4()}"
    first = _runtime(database, DELEGATION, SEND, tenant=tenant)
    paused = await first.run(_request(tenant))
    assert paused.status == RunStatus.AWAITING_APPROVAL, paused.error
    await first.shutdown(grace_seconds=5)

    sent: list[str] = []
    second = _runtime(database, "child sent it", "parent done", sent=sent, tenant=tenant)
    assert await second.recover() == 0  # both runs are paused: nothing to drive
    parent = await second.status(paused.run_id, tenant)
    pause = parent.pause_payload or {}
    child_id = str(pause["child_run_id"])
    assert parent.delegated_runs == {pause["subagent_call_id"]: child_id}
    child = await second.status(child_id, tenant)
    assert child.status == RunStatus.AWAITING_APPROVAL
    assert pause["approval_id"] == (child.pause_payload or {})["approval_id"]
    history = await second.events.history(parent.id)
    assert history[-1].type == "approval.required"
    assert history[-1].data["child_run_id"] == child_id

    await second.resume(parent.id, tenant, {"decision": "approved"}, decided_by="carol")
    final = await second.wait(parent.id, tenant)

    assert final.status == RunStatus.COMPLETED, final.error
    assert final.output == "parent done"
    assert sent == ["bob"]
    approval = await second.approvals.get(str(pause["approval_id"]), tenant)
    assert approval is not None
    assert (approval.status, approval.decided_by) == (ApprovalStatus.APPROVED, "carol")
    done = await second.status(parent.id, tenant)
    assert done.delegated_runs == {}
    [delegated] = [m for m in done.messages if m.role == Role.TOOL and m.name == "core.subagent"]
    assert json.loads(delegated.text_content)["output"] == "child sent it"
    await second.shutdown(grace_seconds=5)


async def test_a_question_survives_a_restart(database: PostgresDatabase) -> None:
    tenant = f"tenant-{uuid4()}"
    first = _runtime(
        database, tool_call_response("core.ask_user", {"question": "Which?"}), tenant=tenant
    )
    paused = await first.run(_request(tenant))
    assert paused.status == RunStatus.AWAITING_CLARIFICATION, paused.error
    await first.shutdown(grace_seconds=5)

    second = _runtime(database, "the first one it is", tenant=tenant)
    state = await second.status(paused.run_id, tenant)
    assert (state.pause_payload or {})["kind"] == "ask_user"
    await second.resume(paused.run_id, tenant, {"clarification": "the first"})
    final = await second.wait(paused.run_id, tenant)

    assert final.status == RunStatus.COMPLETED, final.error
    await second.shutdown(grace_seconds=5)


async def test_a_recovered_run_waits_for_credentials_and_the_flag_alone_is_stored(
    database: PostgresDatabase,
) -> None:
    tenant = f"tenant-{uuid4()}"
    secret = f"cred-{uuid4()}"
    blocked = asyncio.Event()

    class BlockingProvider(MockModelProvider):
        async def generate(self, model_request: Any) -> ModelResponse:
            blocked.set()
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    first = _runtime(database, tenant=tenant)
    first.router._providers["mock"] = BlockingProvider([])
    started = await first.start(_request(tenant), credentials={"authorization": secret})
    await asyncio.wait_for(blocked.wait(), 5)
    await first.shutdown(grace_seconds=0)
    row = await database.fetchrow(
        "SELECT state::text AS state FROM algen_agent_runtime_runs WHERE id=$1", started.id
    )
    assert secret not in row["state"]
    assert json.loads(row["state"])["caller_credentials"] is True

    second = _runtime(database, tool_call_response("api.read", {}), "read it", tenant=tenant)
    await second.recover()
    paused = await second.status(started.id, tenant)
    assert paused.status == RunStatus.AWAITING_CREDENTIALS
    assert (await second.events.history(started.id))[-1].type == "credentials.required"
    await second.shutdown(grace_seconds=5)

    third = _runtime(database, tool_call_response("api.read", {}), "read it", tenant=tenant)
    assert await third.recover() == 0  # still waiting: a paused run is not driven
    await third.resume(started.id, tenant, {}, credentials={"authorization": "fresh"})
    final = await third.wait(started.id, tenant)

    assert final.status == RunStatus.COMPLETED, final.error
    done = await third.status(started.id, tenant)
    [read] = [m for m in done.messages if m.role == Role.TOOL and m.name == "api.read"]
    assert json.loads(read.text_content) == {"signed_in": True}
    await third.shutdown(grace_seconds=5)


async def test_follow_spans_a_pause_on_postgres_events(database: PostgresDatabase) -> None:
    tenant = f"tenant-{uuid4()}"
    runtime = _runtime(
        database, tool_call_response("core.ask_user", {"question": "Which?"}), "ok", tenant=tenant
    )
    client = AlgenAgentRuntimeClient(runtime)
    state = await runtime.start(_request(tenant))
    seen: list[str] = []

    async def follow() -> None:
        async for event in client.follow(state.id):
            seen.append(event.type)

    task = asyncio.create_task(follow())
    await runtime.wait(state.id, tenant)
    await asyncio.sleep(0.6)  # past a history poll of the PostgreSQL bus
    assert not task.done() and seen[-1] == "clarification.required"

    await runtime.resume(state.id, tenant, {"clarification": "the first"})
    await asyncio.wait_for(task, 10)

    assert seen[-1] == "run.completed"
    await runtime.shutdown(grace_seconds=5)


async def test_a_slow_subscriber_reads_on_from_the_durable_history(
    database: PostgresDatabase,
) -> None:
    run_id = f"run-{uuid4()}"
    tenant = f"tenant-{uuid4()}"
    await PostgresRunStore(database).create(
        RunState(
            id=run_id,
            request=RunRequest(agent="a", input="q", tenant_id=tenant, user_id="u"),
        )
    )
    bus = PostgresEventBus(database, subscriber_queue_size=2)
    iterator = bus.subscribe(run_id).__aiter__()

    def event(sequence: int, kind: str = "model.delta") -> RunEvent:
        return RunEvent(
            type=kind,  # type: ignore[arg-type]
            run_id=run_id,
            tenant_id=tenant,
            session_id="s",
            correlation_id="c",
            sequence=sequence,
            data={},
        )

    await bus.publish(event(1))
    assert (await iterator.__anext__()).sequence == 1
    for sequence in range(2, 21):
        await bus.publish(event(sequence, "run.completed" if sequence == 20 else "model.delta"))

    rest = [await asyncio.wait_for(iterator.__anext__(), 5) for _ in range(19)]

    assert [item.sequence for item in rest] == list(range(2, 21))
    assert rest[-1].type == "run.completed"
    await iterator.aclose()  # type: ignore[attr-defined]

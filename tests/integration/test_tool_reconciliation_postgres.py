"""Reconciliation, compensation and crash-between-writes on live PostgreSQL stores."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.exceptions.errors import ConflictError, NotFoundError
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.persistence.postgres import (
    PostgresDatabase,
    PostgresRunStore,
    PostgresToolExecutionStore,
)
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionStatus,
    ToolResult,
)
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.types.contracts import (
    ApprovalPolicy,
    Role,
    RunRequest,
    RunState,
    RunStatus,
    ToolPolicy,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("POSTGRES_DSN"), reason="reconciliation SQL requires a live PostgreSQL"
    ),
]

TOOL = "crm.update"


@pytest.fixture
async def database() -> AsyncIterator[PostgresDatabase]:
    pytest.importorskip("asyncpg")
    db = PostgresDatabase(os.environ["POSTGRES_DSN"])
    await db.start()
    try:
        yield db
    finally:
        await db.close()


def write_tool(handler: Any, *, compensation: Any = None) -> Tool:
    return Tool(
        ToolDefinition(
            name=TOOL,
            version="1",
            description="update an account",
            input_schema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            output_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
            side_effect=SideEffect.EXTERNAL,
            idempotency=Idempotency.KEYED,
            retry_policy={"max_attempts": 1},
        ),
        handler,
        compensation,
    )


def postgres_runtime(
    db: PostgresDatabase,
    provider: MockModelProvider,
    tool: Tool,
    *,
    runs: PostgresRunStore | None = None,
    **tool_policy: Any,
) -> AgentRuntime:
    runtime = make_runtime(
        provider,
        make_agent(
            enabled_tools=frozenset({TOOL}),
            approval_policy=ApprovalPolicy(require_for_side_effects=False),
            tool_policy=ToolPolicy(**tool_policy),
        ),
    )
    runtime.runs = runs or PostgresRunStore(db)
    runtime.tool_executor = ToolExecutor(
        runtime.tools, runtime.policies, PostgresToolExecutionStore(db)
    )
    runtime.tools.register(tool)
    return runtime


def broken(arguments: dict[str, Any], context: ToolContext) -> Any:
    raise ConnectionError("reset after submit")


def request(tenant: str) -> RunRequest:
    return RunRequest(agent="test-agent", input="update a", tenant_id=tenant, user_id="user-1")


def tenant() -> str:
    return f"tenant-{uuid4().hex}"


async def test_list_filters_and_tenant_isolation(database: PostgresDatabase) -> None:
    store = PostgresToolExecutionStore(database)
    runs = PostgresRunStore(database)
    owner, other = tenant(), tenant()
    run_a = RunState(request=request(owner))
    run_b = RunState(request=request(other))
    await runs.create(run_a)
    await runs.create(run_b)

    def context(key: str, state: RunState) -> ToolContext:
        return ToolContext(
            run_id=state.id,
            step_id="tool-1",
            tenant_id=state.request.tenant_id,
            user_id="user-1",
            permissions=frozenset(),
            idempotency_key=key,
        )

    keys = [f"{run_a.id}:{n}" for n in range(3)]
    await store.begin(context(keys[0], run_a), TOOL)
    await store.fail(keys[0], "ConnectionError", indeterminate=True)
    await store.begin(context(keys[1], run_a), "other.tool")
    await store.fail(keys[1], "ConnectionError", indeterminate=True)
    await store.begin(context(keys[2], run_a), TOOL)
    await store.complete(keys[2], ToolResult(value={"ok": True}))
    other_key = f"{run_b.id}:0"
    await store.begin(context(other_key, run_b), TOOL)
    await store.fail(other_key, "ConnectionError", indeterminate=True)

    indeterminate = {ToolExecutionStatus.INDETERMINATE}
    found = await store.find(owner, statuses=indeterminate)
    assert [r.idempotency_key for r in found] == keys[:2]
    assert all(r.created_at is not None and r.updated_at is not None for r in found)
    by_tool = await store.find(owner, statuses=indeterminate, tool_name=TOOL)
    assert [r.idempotency_key for r in by_tool] == [keys[0]]
    by_run = await store.find(owner, statuses=indeterminate, run_id=run_b.id)
    assert by_run == ()
    assert await store.find(owner, statuses=indeterminate, min_age=timedelta(hours=1)) == ()
    assert await store.get(other_key, owner) is None

    record = found[0]
    done = record.model_copy(update={"status": ToolExecutionStatus.COMPLETED})
    assert (
        await store.transition(done.model_copy(update={"tenant_id": other}), expected=indeterminate)
        is None
    )
    assert await store.transition(done, expected=indeterminate) is not None
    assert await store.transition(done, expected=indeterminate) is None
    # A late outcome from the call itself never overwrites that decision.
    await store.fail(record.idempotency_key, "TimeoutError", indeterminate=True)
    current = await store.get(record.idempotency_key, owner)
    assert current is not None and current.status == ToolExecutionStatus.COMPLETED


async def test_status_constraint_rejects_unknown_statuses(database: PostgresDatabase) -> None:
    runs = PostgresRunStore(database)
    state = RunState(request=request(tenant()))
    await runs.create(state)
    with pytest.raises(Exception, match="status_check"):
        await database.execute(
            "INSERT INTO algen_agent_runtime_tool_executions "
            "(idempotency_key, run_id, step_id, tenant_id, tool_name, status, record) "
            "VALUES ($1, $2, 's', $3, 't', 'bogus', '{}'::jsonb)",
            f"{state.id}:x",
            state.id,
            state.request.tenant_id,
        )


async def test_paused_run_resumes_after_resolution(database: PostgresDatabase) -> None:
    owner = tenant()
    provider = MockModelProvider([tool_call_response(TOOL, {"id": "a"}), "done"])
    runtime = postgres_runtime(database, provider, write_tool(broken), on_indeterminate="pause")
    result = await runtime.run(request(owner))
    assert result.status == RunStatus.AWAITING_RECONCILIATION
    [record] = await runtime.reconciliation.list(owner)
    with pytest.raises(NotFoundError):
        await runtime.reconciliation.resolve(
            tenant(), record.idempotency_key, status="failed", actor_id="ops", reason="x"
        )
    outcomes = await asyncio.gather(
        runtime.reconciliation.resolve(
            owner,
            record.idempotency_key,
            status="completed",
            actor_id="ops-1",
            reason="seen upstream",
            result={"ok": True},
        ),
        runtime.reconciliation.resolve(
            owner, record.idempotency_key, status="failed", actor_id="ops-2", reason="b"
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(item, ConflictError) for item in outcomes) == 1
    assert (await runtime.wait(result.run_id, owner)).status == RunStatus.COMPLETED
    settled = await runtime.reconciliation.get(owner, record.idempotency_key)
    assert len(settled.resolutions) == 1
    assert settled.resolutions[0].actor_id in {"ops-1", "ops-2"}


async def test_compensation_runs_once_on_postgres(database: PostgresDatabase) -> None:
    owner = tenant()
    calls: list[str] = []

    async def undo(result: ToolResult, context: ToolContext) -> None:
        await asyncio.sleep(0.01)
        calls.append(context.idempotency_key)

    runtime = postgres_runtime(
        database,
        MockModelProvider([tool_call_response(TOOL, {"id": "a"}), "done"]),
        write_tool(lambda a, c: {"ok": True}, compensation=undo),
    )
    result = await runtime.run(request(owner))
    assert result.status == RunStatus.COMPLETED
    [record] = await runtime.reconciliation.list(owner, statuses={ToolExecutionStatus.COMPLETED})
    outcomes = await asyncio.gather(
        *(
            runtime.reconciliation.compensate(
                owner, record.idempotency_key, actor_id=f"ops-{n}", reason="refund"
            )
            for n in range(3)
        ),
        return_exceptions=True,
    )
    assert calls == [record.idempotency_key]
    assert sum(isinstance(item, ConflictError) for item in outcomes) == 2
    final = await runtime.reconciliation.get(owner, record.idempotency_key)
    assert final.status == ToolExecutionStatus.COMPENSATED


class _ProcessDied(BaseException):
    pass


class CrashingRunStore(PostgresRunStore):
    """Dies on the checkpoint that records a tool call as answered."""

    armed = True

    async def save(self, state: RunState, expected_version: int) -> None:
        if (
            self.armed
            and state.status == RunStatus.BUILDING_CONTEXT
            and any(message.role == Role.TOOL for message in state.messages)
        ):
            self.armed = False
            raise _ProcessDied
        await super().save(state, expected_version)


async def test_crash_between_ledger_and_checkpoint_on_postgres(
    database: PostgresDatabase,
) -> None:
    owner = tenant()
    ran: list[str] = []

    def handler(arguments: dict[str, Any], context: ToolContext) -> Any:
        ran.append(context.idempotency_key)
        return {"ok": True}

    runs = CrashingRunStore(database)
    first = postgres_runtime(
        database,
        MockModelProvider([tool_call_response(TOOL, {"id": "a"})]),
        write_tool(handler),
        runs=runs,
    )
    state = await first.start(request(owner))
    with pytest.raises(_ProcessDied):
        await first.wait(state.id, owner)
    checkpoint = await runs.get(state.id, owner)
    assert checkpoint is not None and checkpoint.status == RunStatus.INVOKING_TOOL
    [record] = await PostgresToolExecutionStore(database).find(
        owner, statuses={ToolExecutionStatus.COMPLETED}
    )

    provider = MockModelProvider(["done"])
    second = postgres_runtime(database, provider, write_tool(handler), runs=runs)
    # Only this test's run: other tests' runs may be active in the same database.
    second.runs = _OnlyRun(runs, state.id)  # type: ignore[assignment]
    assert await second.recover() == 1
    result = await second.wait(state.id, owner)
    assert result.status == RunStatus.COMPLETED
    assert ran == [record.idempotency_key]
    final = await runs.get(state.id, owner)
    assert final is not None
    assert sum(message.role == Role.TOOL for message in final.messages) == 1


class _OnlyRun:
    def __init__(self, runs: PostgresRunStore, run_id: str) -> None:
        self._runs = runs
        self._run_id = run_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runs, name)

    async def list_active(self, limit: int = 1000) -> tuple[RunState, ...]:
        return tuple(
            state for state in await self._runs.list_active(limit) if state.id == self._run_id
        )

"""A workflow tool step on the PostgreSQL ledger (found by the long soak).

A ``tool:<name>`` step records its call under the workflow run's id, which is not an agent run;
the ledger's foreign key to the runs table refused the record, so every tool step failed on
PostgreSQL with ``ForeignKeyViolationError``. Migration 024 drops the key; workflow checkpoint
retention deletes a workflow run's ledger records with it.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from algen_agent_runtime.persistence.postgres import PostgresDatabase, PostgresToolExecutionStore
from algen_agent_runtime.persistence.retention import PostgresRetentionSweeper, RetentionPolicy
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.workflows import (
    MultiAgentWorkflowExecutor,
    PostgresWorkflowCheckpointStore,
    WorkflowHookRegistry,
    WorkflowStatus,
)
from algen_agent_runtime.workflows.contracts import WorkflowManifest, WorkflowNode

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("POSTGRES_DSN"), reason="the ledger's schema needs a live PostgreSQL"
    ),
]


@pytest.fixture
async def database() -> AsyncIterator[PostgresDatabase]:
    pytest.importorskip("asyncpg")
    db = PostgresDatabase(os.environ["POSTGRES_DSN"])
    await db.start()
    try:
        yield db
    finally:
        await db.close()


def _executor(db: PostgresDatabase, calls: list[Any]) -> MultiAgentWorkflowExecutor:
    async def lookup(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        calls.append(arguments)
        return {"found": arguments["key"]}

    tools = ToolRegistry()
    tools.register(
        Tool(
            ToolDefinition(
                name="records.lookup",
                version="1",
                description="Look a record up",
                input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
                output_schema={"type": "object"},
                side_effect=SideEffect.READ,
            ),
            lookup,
        )
    )
    runtime = SimpleNamespace(
        tools=tools,
        tool_executor=ToolExecutor(
            tools, CompositePolicyEngine(), executions=PostgresToolExecutionStore(db)
        ),
    )
    return MultiAgentWorkflowExecutor(
        runtime, WorkflowHookRegistry(), store=PostgresWorkflowCheckpointStore(db)
    )


MANIFEST = WorkflowManifest(
    name="lookup",
    version="1.0.0",
    input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
    output_key="found",
    nodes=(
        WorkflowNode(
            id="find",
            kind="handler",
            handler="tool:records.lookup",
            arguments={"key": "{{inputs.key}}"},
            output_key="found",
        ),
    ),
)


async def test_a_tool_step_is_recorded_in_the_postgres_ledger(database: PostgresDatabase) -> None:
    calls: list[Any] = []
    tenant = f"wf-ledger-{uuid4()}"

    state = await _executor(database, calls).run(
        MANIFEST, {"inputs": {"key": "k-1"}}, tenant_id=tenant, user_id="u"
    )

    assert state.status is WorkflowStatus.COMPLETED, state.error
    assert state.values["found"] == {"found": "k-1"}
    assert calls == [{"key": "k-1"}]
    rows = await database.fetch(
        "SELECT run_id, status FROM algen_agent_runtime_tool_executions WHERE tenant_id=$1",
        tenant,
    )
    assert [(row["run_id"], row["status"]) for row in rows] == [(state.id, "completed")]


async def test_workflow_retention_deletes_its_tool_step_records(database: PostgresDatabase) -> None:
    tenant = f"wf-ledger-{uuid4()}"
    state = await _executor(database, []).run(
        MANIFEST, {"inputs": {"key": "k-2"}}, tenant_id=tenant, user_id="u"
    )
    assert state.status is WorkflowStatus.COMPLETED, state.error
    await database.execute(
        "UPDATE algen_agent_runtime_workflow_checkpoints SET updated_at = now() - interval "
        "'3 days' WHERE id=$1",
        state.id,
    )

    await PostgresRetentionSweeper(database).sweep(
        RetentionPolicy(workflow_checkpoints=timedelta(days=1))
    )

    for table, column in (
        ("algen_agent_runtime_workflow_checkpoints", "id"),
        ("algen_agent_runtime_tool_executions", "run_id"),
    ):
        row = await database.fetchrow(
            f"SELECT count(*) AS n FROM {table} WHERE tenant_id=$1 AND {column}=$2",
            tenant,
            state.id,
        )
        assert row["n"] == 0, table

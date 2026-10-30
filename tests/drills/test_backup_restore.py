"""Backup and restore drill: ``pg_dump`` of a live database, restored into a new one, resumed.

The procedure is the one docs/operations.md#backup-and-restore describes: a logical dump taken
while the Runtime serves (``pg_dump`` reads one consistent snapshot), restored with
``pg_restore`` into an empty database, and a Runtime started on the restored database.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from drill_support import (
    AGENT,
    APPROVAL_AGENT,
    TENANT,
    TOOL,
    USER,
    admin_execute,
    database_name,
    fresh_database,
    ledger,
    postgres_container,
    requires_postgres,
    running,
    side_effects,
    until,
    with_database,
)

from algen_agent_runtime.conversations import MessageStatus
from algen_agent_runtime.types.contracts import RunRequest, RunStatus

pytestmark = [pytest.mark.drill, pytest.mark.integration, pytest.mark.postgres, requires_postgres]


async def test_a_restored_backup_resumes_paused_runs_with_their_history() -> None:
    container = postgres_container()
    user = urlsplit(os.environ["POSTGRES_DSN"]).username or "postgres"
    async with fresh_database() as dsn:
        async with running(dsn) as process:
            runtime = process.runtime
            done = await runtime.run(
                RunRequest(agent=AGENT, input="before the backup", tenant_id=TENANT, user_id=USER)
            )
            assert done.status is RunStatus.COMPLETED
            paused = await runtime.start(
                RunRequest(agent=APPROVAL_AGENT, input="held", tenant_id=TENANT, user_id=USER)
            )

            async def waiting() -> bool:
                state = await runtime.status(paused.id, TENANT)
                return state.status is RunStatus.AWAITING_APPROVAL

            await until(waiting, "the approval pause")
            conversation = await process.container.conversations.create(
                tenant_id=TENANT, user_id=USER, agent=AGENT
            )
            await process.container.conversations.submit(
                conversation.id, TENANT, USER, "a turn before the backup"
            )

            async def answered() -> bool:
                messages = await process.container.conversations.messages(conversation.id, TENANT)
                return bool(messages) and messages[-1].status is MessageStatus.COMPLETED

            await until(answered, "the conversation turn")
            approval_id = (await runtime.status(paused.id, TENANT)).pause_payload["approval_id"]
            events_before = [event.type for event in await runtime.events.history(paused.id)]

            # Online: the dump is taken while the process serves.
            dump = container.exec("pg_dump", "-U", user, "--format=custom", database_name(dsn))
            assert dump.startswith(b"PGDMP")

        restored_name = f"restored_{uuid4().hex[:12]}"
        await admin_execute(f'CREATE DATABASE "{restored_name}"')
        restored = with_database(os.environ["POSTGRES_DSN"], restored_name)
        try:
            container.exec(
                "pg_restore", "-U", user, "--no-owner", "--exit-on-error", "-d", restored_name,
                stdin=dump,
            )  # fmt: skip
            async with running(restored) as process:
                runtime = process.runtime
                # Everything is there: runs, events, ledger, approvals, conversations.
                assert (await runtime.status(done.run_id, TENANT)).status is RunStatus.COMPLETED
                assert await ledger(restored, done.run_id) == [(TOOL, "completed")]
                assert [
                    event.type for event in await runtime.events.history(paused.id)
                ] == events_before
                approval = await runtime.approvals.get(approval_id, TENANT)
                assert approval is not None and approval.status.value == "pending"
                listed = await runtime.approvals.list(TENANT, run_id=paused.id)
                assert [item.id for item in listed] == [approval_id]
                messages = await process.container.conversations.messages(conversation.id, TENANT)
                assert messages[-1].status is MessageStatus.COMPLETED

                await runtime.resume(paused.id, TENANT, {"decision": "approved"})
                result = await runtime.wait(paused.id, TENANT)
                assert result.status is RunStatus.COMPLETED, result.error
                assert await ledger(restored, paused.id) == [(TOOL, "completed")]
                assert len(await side_effects(restored, paused.id)) == 1
                # The restored database is independent of the original.
                assert (await side_effects(dsn, paused.id)) == []
        finally:
            await admin_execute(f'DROP DATABASE IF EXISTS "{restored_name}" WITH (FORCE)')

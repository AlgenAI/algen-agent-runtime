"""Crash drills: a Runtime process is killed (SIGKILL) and a new one on the same database carries on.

Each drill kills ``drill_child.py`` at a chosen point, starts a new Runtime process on the same
PostgreSQL database and checks that every run ends once, that the tool's external effect
happened exactly once and that the ledger holds one completed call per run.
"""

from __future__ import annotations

import pytest
from drill_support import (
    AGENT,
    APPROVAL_AGENT,
    HEADERS,
    TENANT,
    TOOL,
    USER,
    Child,
    fresh_database,
    ledger,
    query,
    requires_postgres,
    running,
    side_effects,
    until,
)

from algen_agent_runtime.conversations import ConversationApprovalDecision, MessageStatus
from algen_agent_runtime.types.contracts import RunStatus
from algen_agent_runtime.workflows.contracts import WorkflowStatus

pytestmark = [pytest.mark.drill, pytest.mark.integration, pytest.mark.postgres, requires_postgres]


async def _completed_once(dsn: str, process: object, run_id: str) -> None:
    result = await process.runtime.wait(run_id, TENANT)  # type: ignore[attr-defined]
    assert result.status is RunStatus.COMPLETED, result.error
    assert (result.output or "").startswith("done:")
    effects = await side_effects(dsn, run_id)
    assert len(effects) == 1, effects
    assert await ledger(dsn, run_id) == [(TOOL, "completed")]


@pytest.mark.parametrize("block", ["after_tool", "in_tool"])
async def test_a_run_interrupted_by_a_crash_completes_once_on_a_new_process(block: str) -> None:
    async with fresh_database() as dsn:
        child = await Child.spawn(
            dsn, block=[block], runs=[{"agent": AGENT, "input": f"crash {block}"}]
        )
        try:
            (run_id,) = await child.expect("RUN")
            await child.expect("BLOCKED")
        finally:
            await child.kill()
        # The effect happened before the crash; the run did not end.
        assert len(await side_effects(dsn, run_id)) == 1
        expected_ledger = "completed" if block == "after_tool" else "started"
        assert await ledger(dsn, run_id) == [(TOOL, expected_ledger)]

        async with running(dsn) as process:
            await _completed_once(dsn, process, run_id)
            events = [event.type for event in await process.runtime.events.history(run_id)]
            assert events.count("run.started") == 1
            assert events.count("run.completed") == 1


async def test_a_run_paused_for_approval_resumes_on_a_new_process() -> None:
    async with fresh_database() as dsn:
        child = await Child.spawn(dsn, runs=[{"agent": APPROVAL_AGENT, "input": "approve me"}])
        try:
            (run_id,) = await child.expect("PAUSED")
        finally:
            await child.kill()
        assert await side_effects(dsn, run_id) == []

        async with running(dsn) as process:
            runtime = process.runtime
            state = await runtime.status(run_id, TENANT)
            assert state.status is RunStatus.AWAITING_APPROVAL
            approval_id = state.pause_payload["approval_id"]
            pending = await runtime.approvals.get(approval_id, TENANT)
            assert pending is not None and pending.status.value == "pending"
            # Through the API, as an approver would.
            response = await process.client.post(
                f"/v1/runs/{run_id}/approval",
                json={"decision": "approved"},
                headers=HEADERS,
            )
            assert response.status_code == 202, response.text
            await _completed_once(dsn, process, run_id)


async def test_a_workflow_interrupted_by_a_crash_is_recovered_once() -> None:
    async with fresh_database() as dsn:
        child = await Child.spawn(dsn, block=["after_tool"], workflows=["nightly report"])
        try:
            (workflow_id,) = await child.expect("WORKFLOW")
            await child.expect("BLOCKED")
        finally:
            await child.kill()

        async with running(dsn) as process:
            store = process.container.workflow_checkpoints

            async def finished() -> object:
                state = await store.get(workflow_id, TENANT)
                return state if state and state.status is WorkflowStatus.COMPLETED else None

            state = await until(finished, "the recovered workflow")
            assert state.values["report"].startswith("done:")
            # One attempt of the node ran to completion after recovery.
            assert state.nodes["work"].attempts == 1
            (retried,) = state.nodes["work"].run_ids
            await _completed_once(dsn, process, retried)
            # The node declares recovery_policy "retry": recovery starts the node again as a
            # new agent run. The interrupted agent run is recovered by the runtime too and
            # completes from its own ledger, so its effect is not repeated, but the retried
            # attempt makes its own: a retried node is at-least-once, which is what declaring
            # it retry-safe accepts (docs/operations.md#restart-recovery).
            rows = await query(
                dsn,
                "SELECT id FROM algen_agent_runtime_runs WHERE id <> $1 "
                "AND state->'request'->>'workflow_run_id' = $2",
                retried,
                workflow_id,
            )
            (interrupted,) = [str(row["id"]) for row in rows]
            await _completed_once(dsn, process, interrupted)
            assert len(await side_effects(dsn)) == 2


async def test_conversation_turns_after_a_crash() -> None:
    """A turn paused for approval resumes on the new process; a turn that was streaming is
    failed as interrupted (retryable), and its run's effect is not repeated."""
    async with fresh_database() as dsn:
        child = await Child.spawn(
            dsn,
            block=["after_tool"],
            turns=[
                {"agent": APPROVAL_AGENT, "input": "approve this turn"},
                {"agent": AGENT, "input": "streaming turn"},
            ],
        )
        try:
            paused_id, streaming_id = await child.expect("TURN", 2)
            await child.expect("BLOCKED")
        finally:
            await child.kill()

        async with running(dsn) as process:
            conversations = process.container.conversations

            async def last(conversation_id: str, *statuses: MessageStatus) -> object:
                messages = await conversations.messages(conversation_id, TENANT)
                return messages[-1] if messages and messages[-1].status in statuses else None

            interrupted = await until(
                lambda: last(streaming_id, MessageStatus.FAILED), "the interrupted turn"
            )
            assert interrupted.metadata["error_type"] == "InterruptedResponse"
            paused = await until(
                lambda: last(paused_id, MessageStatus.AWAITING_APPROVAL), "the paused turn"
            )
            await conversations.respond(
                paused_id,
                paused.pending.turn_id,
                TENANT,
                USER,
                ConversationApprovalDecision(decision="approved"),
            )
            done = await until(
                lambda: last(paused_id, MessageStatus.COMPLETED, MessageStatus.FAILED),
                "the resumed turn",
            )
            assert done.status is MessageStatus.COMPLETED, done
            assert done.id == paused.id
            # Every effect of every run once, however the turns ended.
            effects = await side_effects(dsn)
            assert len(effects) == len(set(effects)) == 2


async def test_recovery_under_repeated_process_failure() -> None:
    """Five processes in a row are killed mid-run; each new one recovers what the last left and
    starts more work. A final process finishes everything: every run once, every effect once."""
    cycles = 5
    async with fresh_database() as dsn:
        started: list[str] = []
        for cycle in range(cycles):
            # Recovered runs stop again after their tool; the new run stops after or inside it.
            block = ["after_tool"] if cycle % 2 == 0 else ["after_tool", "in_tool"]
            child = await Child.spawn(
                dsn, block=block, runs=[{"agent": AGENT, "input": f"cycle {cycle}"}]
            )
            try:
                await child.expect("READY")
                (run_id,) = await child.expect("RUN")
                started.append(run_id)
                # Every run still open (recovered ones too) reaches the gate again.
                await child.expect("BLOCKED", len(started))
            finally:
                await child.kill()

        async with running(dsn) as process:
            for run_id in started:
                await _completed_once(dsn, process, run_id)
            assert len(await side_effects(dsn)) == cycles

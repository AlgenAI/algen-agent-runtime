"""MultiAgentWorkflowExecutor.expire_pause: waits past expiry end through the engine's own path."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.types.contracts import RunStatus, utc_now
from algen_agent_runtime.workflows import (
    MultiAgentWorkflowExecutor,
    WorkflowHookRegistry,
    WorkflowRegistry,
)
from algen_agent_runtime.workflows.contracts import (
    WorkflowApprovalRule,
    WorkflowManifest,
    WorkflowNode,
    WorkflowNodeStatus,
    WorkflowPauseRule,
    WorkflowPredicate,
    WorkflowStatus,
)
from algen_agent_runtime.workflows.store import InMemoryWorkflowCheckpointStore

PAST = "2000-01-01T00:00:00+00:00"


class AskingRuntime:
    """An agent that always asks a question back."""

    async def run(self, request: Any) -> Any:
        return SimpleNamespace(
            run_id="run-1",
            status=RunStatus.COMPLETED,
            output=json.dumps({"status": "unclear", "question": "Which region?"}),
            error=None,
        )


APPROVAL = WorkflowManifest(
    name="release",
    version="1.0.0",
    nodes=(
        WorkflowNode(
            id="review",
            kind="approval",
            output_key="approval",
            approval=WorkflowApprovalRule(prompt="Release?", expires_seconds=60),
        ),
        WorkflowNode(
            id="ship",
            kind="handler",
            handler="ship",
            depends_on=("review",),
            output_key="shipped",
        ),
    ),
)
ASKING = WorkflowManifest(
    name="asking",
    version="1.0.0",
    nodes=(
        WorkflowNode(
            id="route",
            kind="agent",
            agent="router",
            input_template="{{input}}",
            output_key="intent",
            output_schema={"type": "object"},
            pause=WorkflowPauseRule(
                when=WorkflowPredicate(source="intent.status", operator="equals", value="unclear"),
                question_from="intent.question",
            ),
        ),
    ),
)


def _executor(
    store: InMemoryWorkflowCheckpointStore,
    registry: WorkflowRegistry | None = None,
    shipped: list[str] | None = None,
) -> MultiAgentWorkflowExecutor:
    hooks = WorkflowHookRegistry()
    hooks.register_handler("ship", lambda context: (shipped or []).append("x") or "shipped")
    return MultiAgentWorkflowExecutor(
        AskingRuntime(), hooks, store=store, workflow_registry=registry
    )


async def _age(store: InMemoryWorkflowCheckpointStore, workflow_id: str, **pause: str) -> None:
    state = await store.get(workflow_id, "tenant")
    assert state is not None and state.pause is not None
    state.pause.update(pause)
    await store.save(state, state.version)


async def test_an_expired_approval_fails_closed_and_its_dependents_never_run() -> None:
    store = InMemoryWorkflowCheckpointStore()
    shipped: list[str] = []
    executor = _executor(store, shipped=shipped)
    events: list[str] = []
    paused = await executor.run(APPROVAL, {}, tenant_id="tenant", user_id="requester")
    await _age(store, paused.id, expires_at=PAST)
    assert MultiAgentWorkflowExecutor.pause_expired(await executor.get(paused.id, "tenant"))

    expired = await executor.expire_pause(
        APPROVAL, paused.id, tenant_id="tenant", emit=lambda kind, data: events.append(kind)
    )

    assert expired.status is WorkflowStatus.FAILED
    assert expired.nodes["review"].error == "workflow approval expired"
    assert expired.nodes["ship"].status is WorkflowNodeStatus.SKIPPED
    assert shipped == []
    assert events[0] == "workflow.approval.expired" and events[-1] == "workflow.failed"
    # Idempotent: an ended run is left alone, and a late decision is refused.
    again = await executor.expire_pause(APPROVAL, paused.id, tenant_id="tenant")
    assert again.version == expired.version
    with pytest.raises(ConflictError):
        await executor.decide_approval(
            APPROVAL, paused.id, tenant_id="tenant", user_id="late", decision="approved"
        )


async def test_an_approval_within_its_expiry_is_left_waiting() -> None:
    store = InMemoryWorkflowCheckpointStore()
    executor = _executor(store)
    paused = await executor.run(APPROVAL, {}, tenant_id="tenant", user_id="requester")

    same = await executor.expire_pause(APPROVAL, paused.id, tenant_id="tenant")

    assert same.status is WorkflowStatus.AWAITING_APPROVAL
    assert same.version == paused.version


async def test_expiry_is_scoped_to_the_tenant_and_checks_the_manifest() -> None:
    store = InMemoryWorkflowCheckpointStore()
    executor = _executor(store)
    paused = await executor.run(APPROVAL, {}, tenant_id="tenant", user_id="requester")
    await _age(store, paused.id, expires_at=PAST)

    with pytest.raises(Exception, match="not found"):
        await executor.expire_pause(APPROVAL, paused.id, tenant_id="other")
    changed = APPROVAL.model_copy(update={"description": "changed"})
    with pytest.raises(ConflictError):
        await executor.expire_pause(changed, paused.id, tenant_id="tenant")
    assert not MultiAgentWorkflowExecutor.manifest_matches(changed, paused)
    assert MultiAgentWorkflowExecutor.manifest_matches(APPROVAL, paused)


async def test_an_input_wait_expires_only_with_a_timeout() -> None:
    store = InMemoryWorkflowCheckpointStore()
    executor = _executor(store)
    paused = await executor.run(ASKING, {"input": "sales"}, tenant_id="tenant", user_id="u")
    assert paused.status is WorkflowStatus.AWAITING_INPUT

    untouched = await executor.expire_pause(ASKING, paused.id, tenant_id="tenant")
    assert untouched.status is WorkflowStatus.AWAITING_INPUT
    recent = await executor.expire_pause(
        ASKING, paused.id, tenant_id="tenant", input_timeout_seconds=60
    )
    assert recent.status is WorkflowStatus.AWAITING_INPUT

    await _age(store, paused.id, requested_at=(utc_now() - timedelta(minutes=5)).isoformat())
    expired = await executor.expire_pause(
        ASKING, paused.id, tenant_id="tenant", input_timeout_seconds=60
    )

    assert expired.status is WorkflowStatus.FAILED
    assert expired.nodes["route"].status is WorkflowNodeStatus.FAILED
    assert expired.nodes["route"].error == "workflow input expired"
    assert "intent" not in expired.values
    with pytest.raises(ConflictError):
        await executor.resume(ASKING, paused.id, tenant_id="tenant", values={"region": "EU"})


async def test_a_child_workflow_wait_is_expired_in_the_child_and_consumed_by_the_parent() -> None:
    child = APPROVAL.model_copy(update={"name": "release-child", "nodes": APPROVAL.nodes[:1]})
    parent = WorkflowManifest(
        name="release-parent",
        version="1.0.0",
        nodes=(
            WorkflowNode(
                id="dispatch",
                kind="workflow",
                workflow_name=child.name,
                workflow_version=child.version,
                input_template="{{input}}",
                output_key="decision",
            ),
        ),
    )
    asking_child = ASKING.model_copy(update={"name": "asking-child"})
    asking_parent = parent.model_copy(
        update={
            "name": "asking-parent",
            "nodes": (parent.nodes[0].model_copy(update={"workflow_name": asking_child.name}),),
        }
    )
    store = InMemoryWorkflowCheckpointStore()
    registry = WorkflowRegistry()
    registry.register(child, WorkflowHookRegistry())
    registry.register(asking_child, WorkflowHookRegistry())
    executor = _executor(store, registry)

    paused = await executor.run(parent, {"input": "r-42"}, tenant_id="tenant", user_id="u")
    child_id = paused.nodes["dispatch"].child_workflow_ids[0]
    await _age(store, child_id, expires_at=PAST)
    await _age(store, paused.id, expires_at=PAST)
    expired = await executor.expire_pause(parent, paused.id, tenant_id="tenant")
    assert expired.status is WorkflowStatus.FAILED
    assert expired.nodes["dispatch"].status is WorkflowNodeStatus.FAILED
    child_state = await store.get(child_id, "tenant")
    assert child_state is not None and child_state.status is WorkflowStatus.FAILED

    asked = await executor.run(asking_parent, {"input": "x"}, tenant_id="tenant", user_id="u")
    assert asked.status is WorkflowStatus.AWAITING_INPUT
    asking_id = asked.nodes["dispatch"].child_workflow_ids[0]
    old = (utc_now() - timedelta(hours=1)).isoformat()
    await _age(store, asking_id, requested_at=old)
    await _age(store, asked.id, requested_at=old)
    expired = await executor.expire_pause(
        asking_parent, asked.id, tenant_id="tenant", input_timeout_seconds=60
    )
    assert expired.status is WorkflowStatus.FAILED
    asking_state = await store.get(asking_id, "tenant")
    assert asking_state is not None
    assert asking_state.nodes["route"].error == "workflow input expired"

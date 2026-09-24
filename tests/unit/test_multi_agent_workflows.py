from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from algen_agent_runtime.exceptions.errors import ConfigurationError, ConflictError
from algen_agent_runtime.types.contracts import RunStatus, utc_now
from algen_agent_runtime.workflows import (
    MultiAgentWorkflowExecutor,
    WorkflowHookRegistry,
    WorkflowRegistry,
)
from algen_agent_runtime.workflows.contracts import (
    WorkflowApprovalRule,
    WorkflowExecutionState,
    WorkflowManifest,
    WorkflowNode,
    WorkflowNodeExecution,
    WorkflowNodeKind,
    WorkflowNodeStatus,
    WorkflowPauseRule,
    WorkflowPredicate,
    WorkflowResourceReference,
    WorkflowStatus,
)
from algen_agent_runtime.workflows.store import InMemoryWorkflowCheckpointStore


def test_workflow_node_declares_secret_free_runtime_resources() -> None:
    node = WorkflowNode(
        id="query",
        kind="handler",
        handler="analytics.query",
        output_key="rows",
        resources=(
            WorkflowResourceReference(
                kind="query_source",
                name="analytics",
                access="read",
                description="Governed warehouse",
            ),
        ),
    )

    assert node.resources[0].kind.value == "query_source"
    assert node.resources[0].name == "analytics"


def test_workflow_approval_modification_is_opt_in() -> None:
    assert WorkflowApprovalRule(prompt="Review this change").allow_modification is False


class FakeRuntime:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def run(self, request: Any) -> Any:
        self.requests.append(request)
        payload = json.loads(request.input)
        if request.agent == "router":
            output = {"status": "ready", "question": payload["question"]}
        elif request.agent == "planner":
            output = {"queries": [{"id": "a"}, {"id": "b"}]}
        elif request.agent == "sql":
            output = {"task": payload["task"]["id"], "valid": payload["invalid"] is not None}
        else:
            output = {"summary": "verified"}
        return SimpleNamespace(
            run_id=f"run-{len(self.requests)}",
            status=RunStatus.COMPLETED,
            output=json.dumps(output),
            error=None,
        )


def manifest() -> WorkflowManifest:
    return WorkflowManifest(
        name="analyst",
        version="1.0.0",
        nodes=(
            WorkflowNode(
                id="route",
                kind=WorkflowNodeKind.AGENT,
                agent="router",
                input_builder="router_input",
                output_key="intent",
                output_schema={"type": "object"},
            ),
            WorkflowNode(
                id="plan",
                kind=WorkflowNodeKind.AGENT,
                agent="planner",
                input_builder="planner_input",
                output_key="plan",
                output_schema={"type": "object"},
                depends_on=("route",),
            ),
            WorkflowNode(
                id="sql",
                kind=WorkflowNodeKind.MAP_AGENT,
                agent="sql",
                input_builder="sql_input",
                validator="sql_valid",
                max_repairs=1,
                output_key="queries",
                output_schema={"type": "object"},
                map_from="plan.queries",
                max_fan_out=3,
                depends_on=("plan",),
            ),
            WorkflowNode(
                id="execute",
                kind=WorkflowNodeKind.HANDLER,
                handler="execute_queries",
                output_key="results",
                depends_on=("sql",),
            ),
            WorkflowNode(
                id="verify",
                kind=WorkflowNodeKind.AGENT,
                agent="verifier",
                input_builder="verify_input",
                output_key="review",
                output_schema={"type": "object"},
                depends_on=("execute",),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_runtime_workflow_executes_dynamic_fanout_and_bounded_repairs() -> None:
    runtime = FakeRuntime()
    events: list[tuple[str, dict[str, Any]]] = []
    hooks = WorkflowHookRegistry()
    hooks.register_builder(
        "router_input", lambda context: {"question": context.state.values["input"]}
    )
    hooks.register_builder(
        "planner_input", lambda context: {"intent": context.state.values["intent"]}
    )
    hooks.register_builder(
        "sql_input",
        lambda context: {
            "task": context.item,
            "invalid": context.invalid_output,
            "error": context.validation_error,
        },
    )
    hooks.register_validator(
        "sql_valid", lambda value, context: None if value["valid"] else "invalid SQL"
    )
    hooks.register_handler(
        "execute_queries", lambda context: [{"rows": 1} for _ in context.state.values["queries"]]
    )
    hooks.register_builder(
        "verify_input", lambda context: {"results": context.state.values["results"]}
    )

    state = await MultiAgentWorkflowExecutor(runtime, hooks).run(
        manifest(),
        {"input": "question"},
        tenant_id="tenant",
        user_id="user",
        emit=lambda event_type, data: events.append((event_type, data)),
    )

    assert state.status == WorkflowStatus.COMPLETED
    assert len(state.values["queries"]) == 2
    assert state.values["review"]["summary"] == "verified"
    assert state.nodes["sql"].attempts >= 2
    assert all(request.workflow_run_id == state.id for request in runtime.requests)
    sql_requests = [request for request in runtime.requests if request.agent == "sql"]
    repaired = [request for request in sql_requests if request.metadata["workflow_attempt"] == "2"]
    assert all(request.parent_run_id for request in repaired)
    assert any(event_type == "workflow.node.repairing" for event_type, _ in events)


@pytest.mark.asyncio
async def test_runtime_workflow_pauses_for_clarification() -> None:
    runtime = FakeRuntime()
    hooks = WorkflowHookRegistry()
    hooks.register_builder("router_input", lambda context: {"question": "ambiguous"})
    pause_manifest = WorkflowManifest(
        name="clarifying",
        version="1.0.0",
        nodes=(
            WorkflowNode(
                id="route",
                kind=WorkflowNodeKind.AGENT,
                agent="router",
                input_builder="router_input",
                output_key="intent",
                output_schema={"type": "object"},
                pause=WorkflowPauseRule(
                    when=WorkflowPredicate(
                        source="intent.status", operator="equals", value="ready"
                    ),
                    question_from="intent.question",
                ),
            ),
        ),
    )

    state = await MultiAgentWorkflowExecutor(runtime, hooks).run(
        pause_manifest, {}, tenant_id="tenant", user_id="user"
    )

    assert state.status == WorkflowStatus.AWAITING_INPUT
    assert state.pause is not None
    assert state.pause["node_id"] == "route"
    assert state.pause["question"] == "ambiguous"
    assert state.pause["state"] == {}
    assert isinstance(state.pause["requested_at"], str)


@pytest.mark.asyncio
async def test_runtime_workflow_enforces_clarification_limit() -> None:
    runtime = FakeRuntime()
    hooks = WorkflowHookRegistry()
    hooks.register_builder("router_input", lambda context: {"question": "ambiguous"})
    pause_manifest = WorkflowManifest(
        name="clarifying",
        version="1.0.0",
        nodes=(
            WorkflowNode(
                id="route",
                kind=WorkflowNodeKind.AGENT,
                agent="router",
                input_builder="router_input",
                output_key="intent",
                output_schema={"type": "object"},
                pause=WorkflowPauseRule(
                    when=WorkflowPredicate(
                        source="intent.status", operator="equals", value="ready"
                    ),
                    question_from="intent.question",
                    occurrences_from="clarifications",
                    maximum_occurrences=1,
                ),
            ),
        ),
    )

    state = await MultiAgentWorkflowExecutor(runtime, hooks).run(
        pause_manifest,
        {"clarifications": [["question", "answer"]]},
        tenant_id="tenant",
        user_id="user",
    )

    assert state.status == WorkflowStatus.FAILED
    assert "clarification limit" in (state.error or "")


@pytest.mark.asyncio
async def test_runtime_workflow_templates_predicates_joins_loops_and_host_runner() -> None:
    requests: list[Any] = []

    async def run_agent(request: Any, context: Any) -> Any:
        requests.append(request)
        return SimpleNamespace(
            run_id=f"run-{len(requests)}",
            status=RunStatus.COMPLETED,
            output=f"{context.node.id}:{request.metadata['workflow_iteration']}",
            error=None,
        )

    workflow = WorkflowManifest(
        name="runtime-control-flow",
        version="1.0.0",
        hook_provider="example.hooks:create_hooks",
        nodes=(
            WorkflowNode(
                id="route",
                kind=WorkflowNodeKind.PREDICATE,
                condition=WorkflowPredicate(source="input", operator="contains", value="yes"),
                output_key="approved",
            ),
            WorkflowNode(
                id="approved",
                kind=WorkflowNodeKind.AGENT,
                depends_on=("route",),
                agent="worker",
                input_template="approval={{approved}}",
                output_key="approved_output",
                run_if=WorkflowPredicate(source="approved", operator="equals", value=True),
                max_iterations=2,
            ),
            WorkflowNode(
                id="rejected",
                kind=WorkflowNodeKind.AGENT,
                depends_on=("route",),
                agent="worker",
                input_template="rejected",
                output_key="rejected_output",
                run_if=WorkflowPredicate(source="approved", operator="equals", value=False),
            ),
            WorkflowNode(
                id="join",
                kind=WorkflowNodeKind.JOIN,
                depends_on=("approved", "rejected"),
                output_key="result",
                join_strategy="json_array",
            ),
        ),
    )

    state = await MultiAgentWorkflowExecutor(
        FakeRuntime(), WorkflowHookRegistry(), agent_runner=run_agent
    ).run(workflow, {"input": "yes"}, tenant_id="tenant", user_id="user")

    assert state.status == WorkflowStatus.COMPLETED
    assert [request.input for request in requests] == ["approval=True", "approval=True"]
    assert state.values["result"] == ["approved:2"]
    assert state.nodes["approved"].attempts == 2
    assert state.nodes["rejected"].status.value == "skipped"
    assert requests[1].parent_run_id == "run-1"


def test_workflow_manifest_rejects_invalid_contracts_and_unknown_output() -> None:
    node = WorkflowNode(
        id="result",
        kind=WorkflowNodeKind.HANDLER,
        handler="typed.echo",
        output_key="result",
    )
    with pytest.raises(ValueError, match="input_schema is not a valid JSON Schema"):
        WorkflowManifest(
            name="typed",
            version="1.0.0",
            input_schema={"type": "not-a-json-schema-type"},
            nodes=(node,),
        )
    with pytest.raises(ValueError, match="output_schema requires output_key"):
        WorkflowManifest(
            name="typed",
            version="1.0.0",
            output_schema={"type": "object"},
            nodes=(node,),
        )
    with pytest.raises(ValueError, match="not produced"):
        WorkflowManifest(
            name="typed",
            version="1.0.0",
            output_key="unknown",
            nodes=(node,),
        )


@pytest.mark.asyncio
async def test_runtime_workflow_validates_typed_input_and_output_contracts() -> None:
    hooks = WorkflowHookRegistry()
    hooks.register_handler("typed.echo", lambda context: context.state.values["inputs"])
    workflow = WorkflowManifest(
        name="typed",
        version="1.0.0",
        input_schema={
            "type": "object",
            "properties": {"count": {"type": "integer", "minimum": 1}},
            "required": ["count"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
        output_key="result",
        nodes=(
            WorkflowNode(
                id="result",
                kind=WorkflowNodeKind.HANDLER,
                handler="typed.echo",
                output_key="result",
            ),
        ),
    )

    completed = await MultiAgentWorkflowExecutor(FakeRuntime(), hooks).run(
        workflow,
        {"inputs": {"count": 2}},
        tenant_id="tenant",
        user_id="user",
    )
    failed = await MultiAgentWorkflowExecutor(FakeRuntime(), hooks).run(
        workflow,
        {"inputs": {"count": 0}},
        tenant_id="tenant",
        user_id="user",
    )

    assert completed.status == WorkflowStatus.COMPLETED
    assert completed.values["result"] == {"count": 2}
    assert failed.status == WorkflowStatus.FAILED
    assert "workflow input validation failed at count" in (failed.error or "")


@pytest.mark.asyncio
async def test_runtime_workflow_fails_when_typed_output_violates_contract() -> None:
    hooks = WorkflowHookRegistry()
    hooks.register_handler("typed.invalid", lambda context: {"count": "not-an-integer"})
    workflow = WorkflowManifest(
        name="typed-output",
        version="1.0.0",
        output_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
        output_key="result",
        nodes=(
            WorkflowNode(
                id="result",
                kind=WorkflowNodeKind.HANDLER,
                handler="typed.invalid",
                output_key="result",
            ),
        ),
    )

    state = await MultiAgentWorkflowExecutor(FakeRuntime(), hooks).run(
        workflow, {}, tenant_id="tenant", user_id="user"
    )

    assert state.status == WorkflowStatus.FAILED
    assert "workflow output validation failed at count" in (state.error or "")


@pytest.mark.asyncio
async def test_workflow_resume_replays_only_paused_node_and_preserves_completed_checkpoint() -> (
    None
):
    calls = {"prepare": 0, "route": 0, "finish": 0}
    hooks = WorkflowHookRegistry()

    def prepare(context: Any) -> str:
        calls["prepare"] += 1
        return "prepared"

    def route(context: Any) -> dict[str, str]:
        calls["route"] += 1
        clarifications = context.state.values.get("clarifications", [])
        return {
            "status": "ready" if clarifications else "needs_clarification",
            "question": "Which role?",
        }

    def finish(context: Any) -> str:
        calls["finish"] += 1
        return "done"

    hooks.register_handler("prepare", prepare)
    hooks.register_handler("route", route)
    hooks.register_handler("finish", finish)
    workflow = WorkflowManifest(
        name="durable-resume",
        version="1.0.0",
        nodes=(
            WorkflowNode(id="prepare", kind="handler", handler="prepare", output_key="prepared"),
            WorkflowNode(
                id="route",
                kind="handler",
                handler="route",
                output_key="intent",
                depends_on=("prepare",),
                pause=WorkflowPauseRule(
                    when=WorkflowPredicate(
                        source="intent.status", operator="equals", value="needs_clarification"
                    ),
                    question_from="intent.question",
                ),
            ),
            WorkflowNode(
                id="finish",
                kind="handler",
                handler="finish",
                output_key="result",
                depends_on=("route",),
            ),
        ),
    )
    store = InMemoryWorkflowCheckpointStore()
    executor = MultiAgentWorkflowExecutor(FakeRuntime(), hooks, store=store)

    paused = await executor.run(
        workflow,
        {"clarifications": []},
        tenant_id="tenant",
        user_id="user",
        correlation_id="workflow-resume",
    )
    resumed = await executor.resume(
        workflow,
        paused.id,
        tenant_id="tenant",
        values={"clarifications": [("Which role?", "Junior engineer")]},
    )

    assert paused.status is WorkflowStatus.AWAITING_INPUT
    assert resumed.status is WorkflowStatus.COMPLETED
    assert calls == {"prepare": 1, "route": 2, "finish": 1}
    assert resumed.version > paused.version
    assert resumed.deadline_at > paused.deadline_at
    persisted = await store.get(resumed.id, "tenant")
    assert persisted is not None
    assert persisted.status is WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_workflow_recovery_retries_only_explicitly_safe_interrupted_nodes() -> None:
    calls = 0
    hooks = WorkflowHookRegistry()

    def retryable(context: Any) -> str:
        nonlocal calls
        calls += 1
        return "recovered"

    hooks.register_handler("retryable", retryable)
    workflow = WorkflowManifest(
        name="recoverable",
        version="1.0.0",
        nodes=(
            WorkflowNode(
                id="work",
                kind="handler",
                handler="retryable",
                output_key="result",
                recovery_policy="retry",
            ),
        ),
    )
    store = InMemoryWorkflowCheckpointStore()
    state = WorkflowExecutionState(
        id="recoverable-run",
        manifest_name=workflow.name,
        manifest_version=workflow.version,
        tenant_id="tenant",
        user_id="user",
        status=WorkflowStatus.RUNNING,
        deadline_at=utc_now() + timedelta(minutes=1),
        nodes={
            "work": {
                "node_id": "work",
                "status": "running",
                "started_at": utc_now(),
            }
        },
    )
    await store.create(state)

    recovered = await MultiAgentWorkflowExecutor(FakeRuntime(), hooks, store=store).recover(
        workflow, state.id, tenant_id="tenant"
    )

    assert recovered.status is WorkflowStatus.COMPLETED
    assert recovered.values["result"] == "recovered"
    assert calls == 1


@pytest.mark.asyncio
async def test_workflow_recovery_fails_closed_for_unsafe_interrupted_node() -> None:
    workflow = WorkflowManifest(
        name="unsafe-recovery",
        version="1.0.0",
        nodes=(
            WorkflowNode(
                id="send",
                kind="handler",
                handler="send_email",
                output_key="result",
            ),
            WorkflowNode(
                id="after-send",
                kind="handler",
                handler="record_result",
                output_key="recorded",
                depends_on=("send",),
            ),
        ),
    )
    store = InMemoryWorkflowCheckpointStore()
    state = WorkflowExecutionState(
        id="unsafe-run",
        manifest_name=workflow.name,
        manifest_version=workflow.version,
        tenant_id="tenant",
        user_id="user",
        status=WorkflowStatus.RUNNING,
        deadline_at=utc_now() + timedelta(minutes=1),
        nodes={
            "send": {"node_id": "send", "status": "running"},
            "after-send": {"node_id": "after-send", "status": "pending"},
        },
    )
    await store.create(state)

    recovered = await MultiAgentWorkflowExecutor(
        FakeRuntime(), WorkflowHookRegistry(), store=store
    ).recover(workflow, state.id, tenant_id="tenant")

    assert recovered.status is WorkflowStatus.FAILED
    assert "recovery refused unsafe nodes" in (recovered.error or "")
    assert recovered.nodes["after-send"].status.value == "skipped"


@pytest.mark.asyncio
async def test_workflow_resume_rejects_changed_manifest() -> None:
    hooks = WorkflowHookRegistry()
    hooks.register_handler(
        "pause",
        lambda context: {"status": "needs_clarification", "question": "Which role?"},
    )
    workflow = WorkflowManifest(
        name="manifest-identity",
        version="1.0.0",
        nodes=(
            WorkflowNode(
                id="pause",
                kind="handler",
                handler="pause",
                output_key="intent",
                pause=WorkflowPauseRule(
                    when=WorkflowPredicate(
                        source="intent.status",
                        operator="equals",
                        value="needs_clarification",
                    ),
                    question_from="intent.question",
                ),
            ),
        ),
    )
    executor = MultiAgentWorkflowExecutor(FakeRuntime(), hooks)
    paused = await executor.run(workflow, {}, tenant_id="tenant", user_id="user")

    with pytest.raises(ConflictError, match="fingerprint"):
        await executor.resume(
            workflow.model_copy(update={"description": "changed without a version bump"}),
            paused.id,
            tenant_id="tenant",
        )


@pytest.mark.asyncio
async def test_workflow_checkpoint_store_is_tenant_scoped_and_optimistic() -> None:
    store = InMemoryWorkflowCheckpointStore()
    state = WorkflowExecutionState(
        id="checkpoint",
        manifest_name="durable",
        manifest_version="1.0.0",
        tenant_id="tenant-a",
        user_id="user",
    )
    stale = state.model_copy(deep=True)
    await store.create(state)
    other_tenant = state.model_copy(update={"tenant_id": "tenant-b"}, deep=True)
    await store.create(other_tenant)

    assert (await store.get(state.id, "tenant-b")) == other_tenant
    await store.save(state, expected_version=0)
    assert state.version == 1
    with pytest.raises(ConflictError, match="concurrently modified"):
        await store.save(stale, expected_version=0)


@pytest.mark.asyncio
async def test_workflow_approval_pauses_and_applies_approved_parameters() -> None:
    calls: list[dict[str, Any]] = []
    hooks = WorkflowHookRegistry()
    hooks.register_handler(
        "prepare",
        lambda context: {
            "summary": "Send the reviewed candidate response",
            "parameters": {"recipient": "candidate@example.com", "template": "decline-v2"},
        },
    )

    def deliver(context: Any) -> str:
        parameters = context.state.values["approval"]["parameters"]
        calls.append(parameters)
        return "sent"

    hooks.register_handler("deliver", deliver)
    workflow = WorkflowManifest(
        name="approval-flow",
        version="1.0.0",
        nodes=(
            WorkflowNode(
                id="prepare",
                kind="handler",
                handler="prepare",
                output_key="proposal",
                recovery_policy="retry",
            ),
            WorkflowNode(
                id="review",
                kind="approval",
                depends_on=("prepare",),
                output_key="approval",
                approval=WorkflowApprovalRule(
                    prompt="Review the candidate response before delivery",
                    review_from=("proposal.summary",),
                    parameters_from="proposal.parameters",
                    parameters_schema={
                        "type": "object",
                        "required": ["recipient", "template"],
                        "properties": {
                            "recipient": {"type": "string", "format": "email"},
                            "template": {"type": "string"},
                        },
                    },
                    allow_modification=True,
                    expires_seconds=600,
                ),
            ),
            WorkflowNode(
                id="deliver",
                kind="handler",
                handler="deliver",
                output_key="delivery",
                depends_on=("review",),
            ),
        ),
    )
    executor = MultiAgentWorkflowExecutor(FakeRuntime(), hooks)

    paused = await executor.run(workflow, {}, tenant_id="tenant", user_id="requester")

    assert paused.status is WorkflowStatus.AWAITING_APPROVAL
    assert paused.nodes["prepare"].status.value == "completed"
    assert paused.nodes["review"].status.value == "awaiting_approval"
    assert paused.pause is not None
    assert paused.pause["kind"] == "approval"
    assert paused.pause["review"] == {"proposal.summary": "Send the reviewed candidate response"}
    assert paused.pause["redacted_parameters"]["template"] == "decline-v2"

    with pytest.raises(ConfigurationError, match="approval parameters"):
        await executor.decide_approval(
            workflow,
            paused.id,
            tenant_id="tenant",
            user_id="reviewer@example.com",
            decision="modified",
            modified_parameters={"recipient": "candidate@example.com"},
        )

    completed = await executor.decide_approval(
        workflow,
        paused.id,
        tenant_id="tenant",
        user_id="reviewer@example.com",
        decision="modified",
        modified_parameters={
            "recipient": "candidate@example.com",
            "template": "decline-v3",
        },
        comment="Use the reviewed wording",
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.values["approval"]["decision"] == "modified"
    assert completed.values["approval"]["decided_by"] == "reviewer@example.com"
    assert completed.values["approval"]["comment"] == "Use the reviewed wording"
    assert calls == [{"recipient": "candidate@example.com", "template": "decline-v3"}]


@pytest.mark.asyncio
async def test_workflow_approval_rejection_skips_side_effect_by_default() -> None:
    calls = 0
    hooks = WorkflowHookRegistry()
    hooks.register_handler("prepare", lambda context: {"parameters": {"message": "hello"}})

    def side_effect(context: Any) -> str:
        nonlocal calls
        calls += 1
        return "unexpected"

    hooks.register_handler("side_effect", side_effect)
    workflow = WorkflowManifest(
        name="approval-rejection",
        version="1.0.0",
        nodes=(
            WorkflowNode(
                id="prepare",
                kind="handler",
                handler="prepare",
                output_key="proposal",
            ),
            WorkflowNode(
                id="review",
                kind="approval",
                depends_on=("prepare",),
                output_key="approval",
                approval=WorkflowApprovalRule(
                    prompt="Review",
                    parameters_from="proposal.parameters",
                ),
            ),
            WorkflowNode(
                id="send",
                kind="handler",
                handler="side_effect",
                output_key="sent",
                depends_on=("review",),
            ),
        ),
    )
    executor = MultiAgentWorkflowExecutor(FakeRuntime(), hooks)
    paused = await executor.run(workflow, {}, tenant_id="tenant", user_id="requester")

    rejected = await executor.decide_approval(
        workflow,
        paused.id,
        tenant_id="tenant",
        user_id="reviewer",
        decision="rejected",
    )

    assert rejected.status is WorkflowStatus.COMPLETED
    assert rejected.nodes["review"].status.value == "rejected"
    assert rejected.nodes["send"].status.value == "skipped"
    assert rejected.values["approval"]["decision"] == "rejected"
    assert calls == 0


@pytest.mark.asyncio
async def test_workflow_approval_expiry_fails_closed() -> None:
    hooks = WorkflowHookRegistry()
    workflow = WorkflowManifest(
        name="approval-expiry",
        version="1.0.0",
        nodes=(
            WorkflowNode(
                id="review",
                kind="approval",
                output_key="approval",
                approval=WorkflowApprovalRule(prompt="Review", expires_seconds=60),
            ),
        ),
    )
    store = InMemoryWorkflowCheckpointStore()
    executor = MultiAgentWorkflowExecutor(FakeRuntime(), hooks, store=store)
    paused = await executor.run(workflow, {}, tenant_id="tenant", user_id="requester")
    checkpoint = await store.get(paused.id, "tenant")
    assert checkpoint is not None and checkpoint.pause is not None
    checkpoint.pause["expires_at"] = (utc_now() - timedelta(seconds=1)).isoformat()
    await store.save(checkpoint, checkpoint.version)

    expired = await executor.decide_approval(
        workflow,
        paused.id,
        tenant_id="tenant",
        user_id="reviewer",
        decision="approved",
    )

    assert expired.status is WorkflowStatus.FAILED
    assert expired.nodes["review"].status.value == "failed"
    assert expired.nodes["review"].error == "workflow approval expired"


@pytest.mark.asyncio
async def test_parent_workflow_dispatches_version_pinned_child_with_lineage() -> None:
    store = InMemoryWorkflowCheckpointStore()
    child_hooks = WorkflowHookRegistry()
    child_hooks.register_handler(
        "child.execute", lambda context: f"processed:{context.state.values['inputs']['value']}"
    )
    child = WorkflowManifest(
        name="child-flow",
        version="2.1.0",
        input_schema={
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        },
        output_schema={"type": "string"},
        output_key="result",
        nodes=(
            WorkflowNode(
                id="execute",
                kind="handler",
                handler="child.execute",
                output_key="result",
                recovery_policy="retry",
            ),
        ),
    )
    parent_hooks = WorkflowHookRegistry()
    parent_hooks.register_builder(
        "parent.child_input", lambda context: {"value": context.state.values["input"]}
    )
    parent = WorkflowManifest(
        name="parent-flow",
        version="1.0.0",
        output_schema={"type": "string"},
        output_key="child_result",
        nodes=(
            WorkflowNode(
                id="dispatch",
                kind="workflow",
                workflow_name="child-flow",
                workflow_version="2.1.0",
                input_builder="parent.child_input",
                output_key="child_result",
            ),
        ),
    )
    registry = WorkflowRegistry()
    registry.register(child, child_hooks)
    executor = MultiAgentWorkflowExecutor(
        FakeRuntime(), parent_hooks, store=store, workflow_registry=registry
    )

    completed = await executor.run(
        parent,
        {"input": "candidate-42"},
        tenant_id="tenant",
        user_id="operator",
        correlation_id="campaign-7",
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.values["child_result"] == "processed:candidate-42"
    child_id = completed.nodes["dispatch"].child_workflow_ids[0]
    child_state = await store.get(child_id, "tenant")
    assert child_state is not None
    assert child_state.parent_workflow_id == completed.id
    assert child_state.parent_workflow_node_id == "dispatch"
    assert child_state.root_workflow_id == completed.id
    assert child_state.correlation_id == "campaign-7"
    assert child_state.workflow_ancestry == ("parent-flow@1.0.0", "child-flow@2.1.0")


@pytest.mark.asyncio
async def test_child_workflow_approval_is_decided_through_parent_checkpoint() -> None:
    child_hooks = WorkflowHookRegistry()
    child = WorkflowManifest(
        name="review-child",
        version="1.0.0",
        output_key="approval",
        nodes=(
            WorkflowNode(
                id="review",
                kind="approval",
                output_key="approval",
                approval=WorkflowApprovalRule(prompt="Approve the child result"),
            ),
        ),
    )
    parent_hooks = WorkflowHookRegistry()
    parent = WorkflowManifest(
        name="review-parent",
        version="1.0.0",
        output_key="decision",
        nodes=(
            WorkflowNode(
                id="dispatch-review",
                kind="workflow",
                workflow_name="review-child",
                workflow_version="1.0.0",
                input_template="{{input}}",
                output_key="decision",
            ),
        ),
    )
    registry = WorkflowRegistry()
    registry.register(child, child_hooks)
    executor = MultiAgentWorkflowExecutor(FakeRuntime(), parent_hooks, workflow_registry=registry)

    paused = await executor.run(
        parent, {"input": "release-42"}, tenant_id="tenant", user_id="requester"
    )
    assert paused.status is WorkflowStatus.AWAITING_APPROVAL
    assert paused.pause is not None
    assert paused.pause["kind"] == "child_workflow"
    assert paused.pause["prompt"] == "Approve the child result"

    completed = await executor.decide_approval(
        parent,
        paused.id,
        tenant_id="tenant",
        user_id="reviewer",
        decision="approved",
        comment="Reviewed",
    )
    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.values["decision"]["decided_by"] == "reviewer"


@pytest.mark.asyncio
async def test_recovery_expires_nested_child_approval_through_child_checkpoint() -> None:
    store = InMemoryWorkflowCheckpointStore()
    child_hooks = WorkflowHookRegistry()
    child = WorkflowManifest(
        name="expiring-review-child",
        version="1.0.0",
        output_key="approval",
        nodes=(
            WorkflowNode(
                id="review",
                kind="approval",
                output_key="approval",
                approval=WorkflowApprovalRule(prompt="Approve the child result"),
            ),
        ),
    )
    parent_hooks = WorkflowHookRegistry()
    parent = WorkflowManifest(
        name="expiring-review-parent",
        version="1.0.0",
        output_key="decision",
        nodes=(
            WorkflowNode(
                id="dispatch-review",
                kind="workflow",
                workflow_name=child.name,
                workflow_version=child.version,
                input_template="{{input}}",
                output_key="decision",
            ),
        ),
    )
    registry = WorkflowRegistry()
    registry.register(child, child_hooks)
    executor = MultiAgentWorkflowExecutor(
        FakeRuntime(), parent_hooks, store=store, workflow_registry=registry
    )
    paused = await executor.run(
        parent, {"input": "release-42"}, tenant_id="tenant", user_id="requester"
    )
    child_id = paused.nodes["dispatch-review"].child_workflow_ids[0]
    child_state = await store.get(child_id, "tenant")
    assert child_state is not None and child_state.pause is not None
    child_state.pause["expires_at"] = "2000-01-01T00:00:00+00:00"
    await store.save(child_state, child_state.version)

    recovered = await executor.recover(parent, paused.id, tenant_id="tenant")

    assert recovered.status is WorkflowStatus.FAILED
    assert recovered.nodes["dispatch-review"].status is WorkflowNodeStatus.FAILED
    assert "approval expired" in (recovered.error or "")
    persisted_child = await store.get(child_id, "tenant")
    assert persisted_child is not None
    assert persisted_child.status is WorkflowStatus.FAILED


@pytest.mark.asyncio
async def test_child_workflow_dispatch_cycle_fails_closed() -> None:
    hooks_a = WorkflowHookRegistry()
    hooks_b = WorkflowHookRegistry()
    workflow_a = WorkflowManifest(
        name="flow-a",
        version="1.0.0",
        nodes=(
            WorkflowNode(
                id="call-b",
                kind="workflow",
                workflow_name="flow-b",
                workflow_version="1.0.0",
                input_template="{{input}}",
                output_key="b",
            ),
        ),
    )
    workflow_b = WorkflowManifest(
        name="flow-b",
        version="1.0.0",
        nodes=(
            WorkflowNode(
                id="call-a",
                kind="workflow",
                workflow_name="flow-a",
                workflow_version="1.0.0",
                input_template="{{input}}",
                output_key="a",
            ),
        ),
    )
    registry = WorkflowRegistry()
    registry.register(workflow_a, hooks_a)
    registry.register(workflow_b, hooks_b)

    failed = await MultiAgentWorkflowExecutor(
        FakeRuntime(), hooks_a, workflow_registry=registry
    ).run(workflow_a, {"input": "x"}, tenant_id="tenant", user_id="user")

    assert failed.status is WorkflowStatus.FAILED
    assert "dispatch cycle" in (failed.error or "")


@pytest.mark.asyncio
async def test_parent_concurrency_bounds_parallel_child_dispatch() -> None:
    child_hooks = WorkflowHookRegistry()
    active = 0
    maximum_active = 0

    async def bounded_child(context: Any) -> str:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.005)
        active -= 1
        return str(context.state.values["input"])

    child_hooks.register_handler("child.bounded", bounded_child)
    child = WorkflowManifest(
        name="bounded-child",
        version="1.0.0",
        output_key="result",
        nodes=(
            WorkflowNode(
                id="work",
                kind="handler",
                handler="child.bounded",
                output_key="result",
            ),
        ),
    )
    parent = WorkflowManifest(
        name="bounded-parent",
        version="1.0.0",
        maximum_concurrency=1,
        nodes=tuple(
            WorkflowNode(
                id=f"dispatch-{index}",
                kind="workflow",
                workflow_name=child.name,
                workflow_version=child.version,
                input_template=f"child-{index}",
                output_key=f"result_{index}",
            )
            for index in range(2)
        ),
    )
    registry = WorkflowRegistry()
    registry.register(child, child_hooks)
    completed = await MultiAgentWorkflowExecutor(
        FakeRuntime(), WorkflowHookRegistry(), workflow_registry=registry
    ).run(parent, {"input": "x"}, tenant_id="tenant", user_id="user")

    assert completed.status is WorkflowStatus.COMPLETED
    assert maximum_active == 1


@pytest.mark.asyncio
async def test_parent_recovery_reconciles_recorded_child_instead_of_redispatching() -> None:
    store = InMemoryWorkflowCheckpointStore()
    child_hooks = WorkflowHookRegistry()
    calls = 0

    def finish_child(context: Any) -> str:
        nonlocal calls
        calls += 1
        return "recovered-child"

    child_hooks.register_handler("child.finish", finish_child)
    child = WorkflowManifest(
        name="recover-child",
        version="1.0.0",
        output_key="result",
        nodes=(
            WorkflowNode(
                id="finish",
                kind="handler",
                handler="child.finish",
                output_key="result",
                recovery_policy="retry",
            ),
        ),
    )
    parent_hooks = WorkflowHookRegistry()
    parent = WorkflowManifest(
        name="recover-parent",
        version="1.0.0",
        output_key="child_result",
        nodes=(
            WorkflowNode(
                id="dispatch",
                kind="workflow",
                workflow_name="recover-child",
                workflow_version="1.0.0",
                input_template="{{input}}",
                output_key="child_result",
            ),
        ),
    )
    child_id = "child-checkpoint"
    parent_id = "parent-checkpoint"
    await store.create(
        WorkflowExecutionState(
            id=child_id,
            manifest_name=child.name,
            manifest_version=child.version,
            tenant_id="tenant",
            user_id="user",
            status=WorkflowStatus.RUNNING,
            parent_workflow_id=parent_id,
            parent_workflow_node_id="dispatch",
            root_workflow_id=parent_id,
            workflow_depth=1,
            workflow_ancestry=("recover-parent@1.0.0", "recover-child@1.0.0"),
            values={"input": "x"},
            nodes={
                "finish": WorkflowNodeExecution(node_id="finish", status=WorkflowNodeStatus.RUNNING)
            },
        )
    )
    await store.create(
        WorkflowExecutionState(
            id=parent_id,
            manifest_name=parent.name,
            manifest_version=parent.version,
            tenant_id="tenant",
            user_id="user",
            status=WorkflowStatus.RUNNING,
            root_workflow_id=parent_id,
            workflow_ancestry=("recover-parent@1.0.0",),
            values={"input": "x"},
            nodes={
                "dispatch": WorkflowNodeExecution(
                    node_id="dispatch",
                    status=WorkflowNodeStatus.RUNNING,
                    child_workflow_ids=[child_id],
                )
            },
        )
    )
    registry = WorkflowRegistry()
    registry.register(child, child_hooks)
    executor = MultiAgentWorkflowExecutor(
        FakeRuntime(), parent_hooks, store=store, workflow_registry=registry
    )

    recovered = await executor.recover(parent, parent_id, tenant_id="tenant")

    assert recovered.status is WorkflowStatus.COMPLETED
    assert recovered.values["child_result"] == "recovered-child"
    assert recovered.nodes["dispatch"].child_workflow_ids == [child_id]
    assert calls == 1


@pytest.mark.asyncio
async def test_parent_recovery_creates_missing_recorded_child_with_same_identity() -> None:
    store = InMemoryWorkflowCheckpointStore()
    child_hooks = WorkflowHookRegistry()
    child_hooks.register_handler("child.finish", lambda context: "recovered-child")
    child = WorkflowManifest(
        name="checkpointed-child",
        version="1.0.0",
        output_key="result",
        nodes=(
            WorkflowNode(
                id="finish",
                kind="handler",
                handler="child.finish",
                output_key="result",
            ),
        ),
    )
    parent = WorkflowManifest(
        name="checkpointed-parent",
        version="1.0.0",
        output_key="child_result",
        nodes=(
            WorkflowNode(
                id="dispatch",
                kind="workflow",
                workflow_name=child.name,
                workflow_version=child.version,
                input_template="{{input}}",
                output_key="child_result",
            ),
        ),
    )
    child_id = "recorded-before-child-create"
    parent_id = "parent-with-recorded-child"
    await store.create(
        WorkflowExecutionState(
            id=parent_id,
            manifest_name=parent.name,
            manifest_version=parent.version,
            tenant_id="tenant",
            user_id="user",
            status=WorkflowStatus.RUNNING,
            root_workflow_id=parent_id,
            workflow_ancestry=("checkpointed-parent@1.0.0",),
            values={"input": "x"},
            nodes={
                "dispatch": WorkflowNodeExecution(
                    node_id="dispatch",
                    status=WorkflowNodeStatus.RUNNING,
                    child_workflow_ids=[child_id],
                    child_workflow_inputs={
                        child_id: {"input": "x", "question": "x", "clarifications": []}
                    },
                )
            },
        )
    )
    registry = WorkflowRegistry()
    registry.register(child, child_hooks)
    executor = MultiAgentWorkflowExecutor(
        FakeRuntime(), WorkflowHookRegistry(), store=store, workflow_registry=registry
    )

    recovered = await executor.recover(parent, parent_id, tenant_id="tenant")

    assert recovered.status is WorkflowStatus.COMPLETED
    assert recovered.values["child_result"] == "recovered-child"
    assert recovered.nodes["dispatch"].child_workflow_ids == [child_id]
    recovered_child = await store.get(child_id, "tenant")
    assert recovered_child is not None
    assert recovered_child.status is WorkflowStatus.COMPLETED


def test_unresolved_template_placeholder_is_rejected() -> None:
    # 1. Manifest with nested placeholder syntax fails validation with node & placeholder
    with pytest.raises(ValueError, match=r"node 'worker'.*step_one_data\.input"):
        WorkflowManifest(
            name="test-manifest",
            version="1.0.0",
            nodes=(
                WorkflowNode(
                    id="step-one",
                    kind=WorkflowNodeKind.HANDLER,
                    handler="hooks.step_one",
                    output_key="step_one_data",
                ),
                WorkflowNode(
                    id="worker",
                    kind=WorkflowNodeKind.AGENT,
                    depends_on=("step-one",),
                    agent="analyst",
                    input_template="{{step_one_data.input}}",
                    output_key="analysis",
                ),
            ),
        )

    # 2. Manifest with unknown placeholder key fails validation with node & placeholder
    with pytest.raises(ValueError, match=r"node 'worker'.*unknown_key"):
        WorkflowManifest(
            name="test-manifest",
            version="1.0.0",
            nodes=(
                WorkflowNode(
                    id="worker",
                    kind=WorkflowNodeKind.AGENT,
                    agent="analyst",
                    input_template="{{unknown_key}}",
                    output_key="analysis",
                ),
            ),
        )

    # 3. Direct render_template raises typed ConfigurationError when unresolved placeholder remains
    with pytest.raises(ConfigurationError, match=r"node 'worker'.*\{\{missing\}\}"):
        MultiAgentWorkflowExecutor._render_template("Prompt: {{missing}}", {}, None, node="worker")


def test_supported_placeholder_forms_render() -> None:
    # {{item}}, {{key}}, {{key.output}}
    rendered_item = MultiAgentWorkflowExecutor._render_template(
        "Process {{item}}", {}, "batch-42", node="mapper"
    )
    assert rendered_item == "Process batch-42"

    rendered_key = MultiAgentWorkflowExecutor._render_template(
        "Analyze {{task}} and {{result.output}}",
        {"task": "logs", "result": "clean"},
        None,
        node="worker",
    )
    assert rendered_key == "Analyze logs and clean"


def test_default_input_template_still_renders() -> None:
    rendered_default = MultiAgentWorkflowExecutor._render_template(
        "{{input}}",
        {"input": "standard query"},
        None,
        node="worker",
    )
    assert rendered_default == "standard query"

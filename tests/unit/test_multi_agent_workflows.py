from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from algen_agent_runtime.types.contracts import RunStatus
from algen_agent_runtime.workflows import MultiAgentWorkflowExecutor, WorkflowHookRegistry
from algen_agent_runtime.workflows.contracts import (
    WorkflowManifest,
    WorkflowNode,
    WorkflowNodeKind,
    WorkflowPauseRule,
    WorkflowPredicate,
    WorkflowResourceReference,
    WorkflowStatus,
)


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
    assert state.pause == {"node_id": "route", "question": "ambiguous", "state": {}}


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

"""A workflow step that calls a registered tool by name, with no workflow hook code."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import make_runtime
from pydantic import ValidationError

from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolDefinition
from algen_agent_runtime.workflows import MultiAgentWorkflowExecutor, WorkflowHookRegistry
from algen_agent_runtime.workflows.contracts import (
    WorkflowApprovalRule,
    WorkflowManifest,
    WorkflowNode,
    WorkflowNodeKind,
    WorkflowStatus,
)


def _runtime(seen: list[dict[str, Any]]) -> Any:
    runtime = make_runtime()
    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="evidence.collect",
                version="1",
                description="Collect release evidence",
                input_schema={
                    "type": "object",
                    "properties": {
                        "tag": {"type": "string"},
                        "checks": {"type": "array"},
                        "note": {"type": "string"},
                    },
                    "required": ["tag", "checks"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
            ),
            lambda args, ctx: (
                seen.append(args) or {"tag": args["tag"], "count": len(args["checks"])}
            ),
        )
    )
    return runtime


def _workflow(arguments: dict[str, Any]) -> WorkflowManifest:
    return WorkflowManifest(
        name="release",
        version="1.0.0",
        output_key="evidence",
        nodes=(
            WorkflowNode(
                id="evidence",
                kind=WorkflowNodeKind.HANDLER,
                handler="tool:evidence.collect",
                arguments=arguments,
                output_key="evidence",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_a_tool_step_passes_workflow_values_and_stores_the_result() -> None:
    seen: list[dict[str, Any]] = []
    state = await MultiAgentWorkflowExecutor(_runtime(seen), WorkflowHookRegistry()).run(
        _workflow(
            {"tag": "{{inputs.tag}}", "checks": "{{inputs.checks}}", "note": "Release {{label}}"}
        ),
        {"inputs": {"tag": "v1.2", "checks": ["build", "scan"]}, "label": "candidate"},
        tenant_id="tenant",
        user_id="user",
    )
    assert state.status == WorkflowStatus.COMPLETED
    # A whole "{{key}}" keeps the value's type; text placeholders are filled in as text.
    assert seen == [{"tag": "v1.2", "checks": ["build", "scan"], "note": "Release candidate"}]
    assert state.values["evidence"] == {"tag": "v1.2", "count": 2}


@pytest.mark.asyncio
async def test_a_tool_step_reading_a_missing_value_fails_the_step() -> None:
    seen: list[dict[str, Any]] = []
    state = await MultiAgentWorkflowExecutor(_runtime(seen), WorkflowHookRegistry()).run(
        _workflow({"tag": "{{inputs.missing}}", "checks": []}),
        {"inputs": {}},
        tenant_id="tenant",
        user_id="user",
    )
    assert state.status == WorkflowStatus.FAILED
    assert seen == []


def test_only_tool_steps_take_arguments_and_they_must_name_a_tool() -> None:
    with pytest.raises(ValidationError, match="only tool steps"):
        WorkflowNode(
            id="x",
            kind=WorkflowNodeKind.HANDLER,
            handler="hooks.x",
            arguments={"a": 1},
            output_key="x",
        )
    with pytest.raises(ValidationError, match="does not name a valid tool"):
        WorkflowNode(id="x", kind=WorkflowNodeKind.HANDLER, handler="tool:Bad Name", output_key="x")


@pytest.mark.asyncio
async def test_a_tool_step_needs_a_runtime_with_tools() -> None:
    state = await MultiAgentWorkflowExecutor(object(), WorkflowHookRegistry()).run(
        _workflow({"tag": "v", "checks": []}), {}, tenant_id="tenant", user_id="user"
    )
    assert state.status == WorkflowStatus.FAILED
    assert "needs a runtime with tools" in (state.nodes["evidence"].error or state.error or "")


def _approval_runtime(seen: list[dict[str, Any]]) -> Any:
    runtime = make_runtime()
    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="release.publish",
                version="1",
                description="Publish the release",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                side_effect=SideEffect.EXTERNAL,
                requires_approval=True,
            ),
            lambda args, ctx: seen.append(args) or {"published": True},
        )
    )
    return runtime


def _publish_workflow(*, approval: bool, rejection_policy: str = "skip_dependents") -> Any:
    publish = WorkflowNode(
        id="publish",
        kind=WorkflowNodeKind.HANDLER,
        handler="tool:release.publish",
        depends_on=("review",) if approval else (),
        output_key="published",
    )
    nodes: tuple[WorkflowNode, ...] = (publish,)
    if approval:
        review = WorkflowNode(
            id="review",
            kind=WorkflowNodeKind.APPROVAL,
            output_key="review",
            approval=WorkflowApprovalRule(prompt="Publish?", rejection_policy=rejection_policy),
        )
        nodes = (review, publish)
    return WorkflowManifest(name="publish", version="1.0.0", output_key="published", nodes=nodes)


@pytest.mark.asyncio
async def test_an_approval_required_tool_step_runs_after_its_upstream_approval() -> None:
    seen: list[dict[str, Any]] = []
    executor = MultiAgentWorkflowExecutor(_approval_runtime(seen), WorkflowHookRegistry())
    manifest = _publish_workflow(approval=True)
    paused = await executor.run(manifest, {}, tenant_id="tenant", user_id="user")
    assert paused.status == WorkflowStatus.AWAITING_APPROVAL and seen == []

    done = await executor.decide_approval(
        manifest, paused.id, tenant_id="tenant", user_id="approver", decision="approved"
    )

    assert done.status == WorkflowStatus.COMPLETED
    assert seen == [{}]


@pytest.mark.asyncio
async def test_a_rejected_approval_does_not_cover_an_approval_required_tool_step() -> None:
    """A rejection that lets dependents continue still does not approve the call."""
    seen: list[dict[str, Any]] = []
    executor = MultiAgentWorkflowExecutor(_approval_runtime(seen), WorkflowHookRegistry())
    manifest = _publish_workflow(approval=True, rejection_policy="continue")
    paused = await executor.run(manifest, {}, tenant_id="tenant", user_id="user")

    done = await executor.decide_approval(
        manifest, paused.id, tenant_id="tenant", user_id="approver", decision="rejected"
    )

    assert done.status == WorkflowStatus.FAILED
    assert seen == []
    assert "requires an approval" in (done.nodes["publish"].error or done.error or "")


@pytest.mark.asyncio
async def test_an_approval_required_tool_step_without_an_approval_node_is_refused() -> None:
    seen: list[dict[str, Any]] = []
    executor = MultiAgentWorkflowExecutor(_approval_runtime(seen), WorkflowHookRegistry())

    state = await executor.run(
        _publish_workflow(approval=False), {}, tenant_id="tenant", user_id="user"
    )

    assert state.status == WorkflowStatus.FAILED
    assert seen == []

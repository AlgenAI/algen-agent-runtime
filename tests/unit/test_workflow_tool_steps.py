"""A workflow step that calls a registered tool by name, with no workflow hook code."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import make_runtime
from pydantic import ValidationError

from algen_agent_runtime.tools.contracts import Tool, ToolDefinition
from algen_agent_runtime.workflows import MultiAgentWorkflowExecutor, WorkflowHookRegistry
from algen_agent_runtime.workflows.contracts import (
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

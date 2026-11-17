"""A typed workflow's input properties render in ``input_template`` (found by the long soak).

The manifest accepts ``{{<property>}}`` for a property its ``input_schema`` declares, as
docs/multi-agent-workflows.md says, but the engine kept typed input under ``values["inputs"]`` and
rendered only top-level values, so such a node failed at run time with "unresolved placeholder".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from algen_agent_runtime.types.contracts import RunStatus
from algen_agent_runtime.workflows import MultiAgentWorkflowExecutor, WorkflowHookRegistry
from algen_agent_runtime.workflows.contracts import (
    WorkflowManifest,
    WorkflowNode,
    WorkflowNodeKind,
    WorkflowStatus,
)


class EchoRuntime:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def run(self, request: Any) -> Any:
        self.inputs.append(request.input)
        return SimpleNamespace(
            run_id=f"run-{len(self.inputs)}",
            status=RunStatus.COMPLETED,
            output=f"echo {request.input}",
            error=None,
        )


def typed_manifest(template: str) -> WorkflowManifest:
    return WorkflowManifest(
        name="typed",
        version="1.0.0",
        input_schema={
            "type": "object",
            "required": ["text", "key"],
            "properties": {"text": {"type": "string"}, "key": {"type": "string"}},
        },
        nodes=(
            WorkflowNode(
                id="draft",
                kind=WorkflowNodeKind.AGENT,
                agent="writer",
                input_template=template,
                output_key="draft",
            ),
            WorkflowNode(
                id="review",
                kind=WorkflowNodeKind.AGENT,
                agent="reviewer",
                depends_on=("draft",),
                input_template="{{draft}} for {{key}}",
                output_key="review",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_input_schema_properties_render_in_templates() -> None:
    runtime = EchoRuntime()
    executor = MultiAgentWorkflowExecutor(runtime, WorkflowHookRegistry())

    state = await executor.run(
        typed_manifest("Write about {{text}} ({{key}})"),
        {"inputs": {"text": "tides", "key": "k-1"}},
        tenant_id="tenant",
        user_id="user",
    )

    assert state.status is WorkflowStatus.COMPLETED, state.error
    assert runtime.inputs == ["Write about tides (k-1)", "echo Write about tides (k-1) for k-1"]


@pytest.mark.asyncio
async def test_a_node_output_wins_over_an_input_property_of_the_same_name() -> None:
    runtime = EchoRuntime()
    executor = MultiAgentWorkflowExecutor(runtime, WorkflowHookRegistry())
    manifest = WorkflowManifest(
        name="shadow",
        version="1.0.0",
        input_schema={"type": "object", "properties": {"draft": {"type": "string"}}},
        nodes=(
            WorkflowNode(
                id="first",
                kind=WorkflowNodeKind.AGENT,
                agent="writer",
                input_template="{{draft}}",
                output_key="first",
            ),
            WorkflowNode(
                id="second",
                kind=WorkflowNodeKind.AGENT,
                agent="writer",
                depends_on=("first",),
                input_template="{{first}}",
                output_key="draft",
            ),
        ),
    )

    state = await executor.run(
        manifest, {"inputs": {"draft": "from input"}}, tenant_id="tenant", user_id="user"
    )

    assert state.status is WorkflowStatus.COMPLETED, state.error
    assert runtime.inputs == ["from input", "echo from input"]

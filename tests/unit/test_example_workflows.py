from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.workflows import (
    MultiAgentWorkflowExecutor,
    WorkflowHookRegistry,
    WorkflowStatus,
)
from examples.pattern_provider_fallback.app import deterministic_fallback

ROOT = Path(__file__).parents[2]
PORTABLE_EXAMPLES = (
    "examples/quickstart_tool/agent.yaml",
    "examples/quickstart_approval/agent.yaml",
    "examples/pattern_approval_workflow/agent.yaml",
    "examples/pattern_governed_research/agent.yaml",
    "examples/pattern_langgraph_governance/agent.yaml",
    "examples/pattern_multi_agent_fanout/agent.yaml",
    "examples/pattern_evaluation_gate/agent.yaml",
    "examples/reference_customer_support/config/agent.yaml",
    "examples/reference_incident_response/config/agent.yaml",
    "examples/reference_invoice_exceptions/config/agent.yaml",
)
ALL_STUDIO_CONFIGS = (
    "examples/quickstart_cloud_chat/agent.yaml",
    *PORTABLE_EXAMPLES,
    "examples/case_study_responsible_hiring/config/agent.yaml",
    "examples/case_study_teaching_assistant/config/agent.yaml",
)


def _hooks(reference: str) -> WorkflowHookRegistry:
    module_name, factory_name = reference.split(":", 1)
    result = getattr(importlib.import_module(module_name), factory_name)()
    assert isinstance(result, WorkflowHookRegistry)
    return result


@pytest.mark.asyncio
async def test_provider_fallback_example_has_a_deterministic_failure_path() -> None:
    route, output = await deterministic_fallback("test")
    assert route == "mock/deterministic"
    assert output == "deterministic fallback response"


@pytest.mark.parametrize("relative_path", ALL_STUDIO_CONFIGS)
def test_example_configuration_is_studio_importable(relative_path: str) -> None:
    settings = load_settings((ROOT / relative_path,))
    assert settings.agents
    for workflow in settings.workflows.values():
        assert workflow.hook_provider
        assert isinstance(_hooks(workflow.hook_provider), WorkflowHookRegistry)


@pytest.mark.parametrize("relative_path", PORTABLE_EXAMPLES)
@pytest.mark.asyncio
async def test_portable_example_workflow_runs_without_paid_services(relative_path: str) -> None:
    settings = load_settings((ROOT / relative_path,))
    container = build_container(settings)
    try:
        for workflow in settings.workflows.values():
            assert workflow.hook_provider
            state = await MultiAgentWorkflowExecutor(
                AlgenAgentRuntimeClient(container.runtime),
                _hooks(workflow.hook_provider),
            ).run(
                workflow,
                {
                    "input": "Review this synthetic request",
                    "question": "Review this synthetic request",
                    "clarifications": [("Approved?", "approve")],
                },
                tenant_id="example-tenant",
                user_id="example-user",
            )
            assert state.status == WorkflowStatus.COMPLETED, state.error
            assert all(node.attempts >= 0 for node in state.nodes.values())
    finally:
        container.close()


@pytest.mark.parametrize(
    ("relative_path", "workflow_name"),
    (
        ("examples/quickstart_approval/agent.yaml", "approval-quickstart"),
        ("examples/reference_incident_response/config/agent.yaml", "incident-response"),
        ("examples/reference_invoice_exceptions/config/agent.yaml", "invoice-exception-review"),
    ),
)
@pytest.mark.asyncio
async def test_human_checkpoint_examples_pause_without_a_decision(
    relative_path: str, workflow_name: str
) -> None:
    settings = load_settings((ROOT / relative_path,))
    workflow = settings.workflows[workflow_name]
    container = build_container(settings)
    try:
        state = await MultiAgentWorkflowExecutor(
            AlgenAgentRuntimeClient(container.runtime),
            _hooks(workflow.hook_provider or ""),
        ).run(
            workflow,
            {
                "input": "Review this request",
                "question": "Review this request",
                "clarifications": [],
            },
            tenant_id="example-tenant",
            user_id="example-user",
        )
        assert state.status == WorkflowStatus.AWAITING_INPUT
        assert state.pause and state.pause["question"]
    finally:
        container.close()

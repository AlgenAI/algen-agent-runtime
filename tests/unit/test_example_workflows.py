from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.workflows import (
    MultiAgentWorkflowExecutor,
    WorkflowHookLoader,
    WorkflowHookRegistry,
    WorkflowRegistry,
    WorkflowStatus,
)
from examples.pattern_mcp_governance.app import run_mcp_governance_demonstration
from examples.pattern_openai_agents_integration.app import run_openai_agents_demonstration
from examples.pattern_provider_fallback.app import deterministic_fallback

ROOT = Path(__file__).parents[2]
PORTABLE_EXAMPLES = (
    "examples/quickstart_tool/agent.yaml",
    "examples/quickstart_approval/agent.yaml",
    "examples/pattern_approval_workflow/agent.yaml",
    "examples/pattern_governed_research/agent.yaml",
    "examples/pattern_langgraph_governance/agent.yaml",
    "examples/pattern_multi_agent_fanout/agent.yaml",
    "examples/pattern_child_workflow/agent.yaml",
    "examples/pattern_evaluation_gate/agent.yaml",
    "examples/reference_customer_support/config/agent.yaml",
    "examples/reference_incident_response/config/agent.yaml",
    "examples/reference_invoice_exceptions/config/agent.yaml",
)
ALL_AGENT_CONFIGS = tuple(
    sorted(str(path.relative_to(ROOT)) for path in (ROOT / "examples").glob("**/agent.yaml"))
)


LOADER = WorkflowHookLoader(allowed_modules=("examples",))


def _hooks(reference: str) -> WorkflowHookRegistry:
    loaded = LOADER.load(reference)
    return loaded.hooks


def test_all_agent_configs_discovered() -> None:
    assert len(ALL_AGENT_CONFIGS) >= 19
    assert "examples/pattern_provider_fallback/agent.yaml" in ALL_AGENT_CONFIGS
    assert "examples/quickstart_local_chat/agent.yaml" in ALL_AGENT_CONFIGS


@pytest.mark.asyncio
async def test_provider_fallback_example_has_a_deterministic_failure_path() -> None:
    route, output = await deterministic_fallback("test")
    assert route == "mock/deterministic"
    assert output == "deterministic fallback response"


@pytest.mark.asyncio
async def test_mcp_governance_example_has_correct_policy_boundaries() -> None:
    pytest.importorskip("mcp")
    results = await run_mcp_governance_demonstration()
    assert results["read_tool"]["name"] == "mcp.account_service.read_account"
    assert results["read_tool"]["side_effect"] == "read"
    assert results["read_tool"]["decision"] == "allow"
    assert results["read_tool"]["requires_approval"] is False

    assert results["delete_tool"]["name"] == "mcp.account_service.delete_account"
    assert results["delete_tool"]["side_effect"] == "destructive"
    assert results["delete_tool"]["decision"] == "require_approval"
    assert results["delete_tool"]["requires_approval"] is True


@pytest.mark.asyncio
async def test_openai_agents_integration_example_executes_offline() -> None:
    pytest.importorskip("agents")
    result = await run_openai_agents_demonstration()
    assert result["framework_id"] == "openai_agents"
    assert result["status"] == "completed"
    assert "Log analysis completed" in result["output"]
    assert "started" in result["stream_events"]
    assert "completed" in result["stream_events"]


@pytest.mark.parametrize("relative_path", ALL_AGENT_CONFIGS)
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
        registry = WorkflowRegistry()
        for configured in settings.workflows.values():
            assert configured.hook_provider
            registry.register(configured, _hooks(configured.hook_provider))
        for workflow in settings.workflows.values():
            assert workflow.hook_provider
            executor = MultiAgentWorkflowExecutor(
                AlgenAgentRuntimeClient(container.runtime),
                registry.resolve(workflow.name, workflow.version)[1],
                store=container.workflow_checkpoints,
                workflow_registry=registry,
            )
            initial_values: dict[str, Any] = {
                "input": "Review this synthetic request",
                "question": "Review this synthetic request",
                "clarifications": [("Approved?", "approve")],
            }
            if workflow.input_schema is not None:
                initial_values = {
                    "inputs": {
                        key: "Review this synthetic request"
                        for key in workflow.input_schema.get("required", [])
                    },
                    "clarifications": [],
                }
            state = await executor.run(
                workflow,
                initial_values,
                tenant_id="example-tenant",
                user_id="example-user",
            )
            while state.status in {
                WorkflowStatus.AWAITING_INPUT,
                WorkflowStatus.AWAITING_APPROVAL,
            }:
                if state.status is WorkflowStatus.AWAITING_APPROVAL:
                    state = await executor.decide_approval(
                        workflow,
                        state.id,
                        tenant_id="example-tenant",
                        user_id="example-reviewer",
                        decision="approved",
                    )
                else:
                    state = await executor.resume(
                        workflow,
                        state.id,
                        tenant_id="example-tenant",
                        values={"clarifications": [("Approved?", "approve")]},
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
        assert state.status in {
            WorkflowStatus.AWAITING_INPUT,
            WorkflowStatus.AWAITING_APPROVAL,
        }
        assert state.pause
        assert state.pause.get("question") or state.pause.get("prompt")
    finally:
        container.close()

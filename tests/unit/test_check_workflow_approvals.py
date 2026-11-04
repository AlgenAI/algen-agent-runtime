"""``check`` warns when an approval-required tool step's call is not what the approver saw."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from algen_agent_runtime.orchestration.check import workflow_approval_issues
from algen_agent_runtime.workflows.contracts import (
    WorkflowApprovalRule,
    WorkflowManifest,
    WorkflowNode,
    WorkflowNodeKind,
)

TOOLS = (
    SimpleNamespace(name="payments.send", requires_approval=True),
    SimpleNamespace(name="payments.quote", requires_approval=False),
)


def _workflow(*, tool: str = "payments.send", modifiable: bool = False, **arguments: Any) -> Any:
    return {
        "pay": WorkflowManifest(
            name="pay",
            version="1.0.0",
            nodes=(
                WorkflowNode(
                    id="draft", kind=WorkflowNodeKind.HANDLER, handler="draft", output_key="draft"
                ),
                WorkflowNode(
                    id="approve",
                    kind=WorkflowNodeKind.APPROVAL,
                    depends_on=("draft",),
                    output_key="approval",
                    approval=WorkflowApprovalRule(
                        prompt="Send?",
                        review_from=("draft.summary",),
                        parameters_from="draft.payment",
                        allow_modification=modifiable,
                    ),
                ),
                WorkflowNode(
                    id="pay",
                    kind=WorkflowNodeKind.HANDLER,
                    handler=f"tool:{tool}",
                    depends_on=("approve",),
                    arguments=arguments,
                    output_key="paid",
                ),
            ),
        )
    }


def _codes(workflows: Any) -> list[str]:
    return [issue.code for issue in workflow_approval_issues(workflows, TOOLS)]


def test_arguments_read_from_what_the_approver_saw_pass() -> None:
    assert _codes(_workflow(to="{{draft.payment.to}}", note="Paid {{approval}}", fixed=1)) == []
    assert _codes(_workflow(modifiable=True, amount="{{approval.parameters.amount}}")) == []


def test_arguments_the_approver_did_not_see_are_reported() -> None:
    issues = list(
        workflow_approval_issues(
            _workflow(to="{{draft.payment.to}}", memo="{{draft.memo}}", tag="{{inputs.tag}}"),
            TOOLS,
        )
    )
    assert [issue.code for issue in issues] == ["APPROVAL_ARGUMENTS_NOT_REVIEWED"]
    assert issues[0].severity == "warning"
    assert "['draft.memo', 'inputs.tag']" in issues[0].message
    assert issues[0].location == "workflows.pay.nodes.pay"
    assert "{{approval.parameters.<field>}}" in (issues[0].suggestion or "")


def test_a_modifiable_approval_whose_decision_the_step_ignores_is_reported() -> None:
    assert _codes(_workflow(modifiable=True, to="{{draft.payment.to}}")) == [
        "APPROVAL_MODIFICATION_IGNORED"
    ]


def test_tools_without_approval_and_unknown_tools_are_not_checked() -> None:
    assert _codes(_workflow(tool="payments.quote", memo="{{draft.memo}}")) == []
    assert _codes(_workflow(tool="mcp.unknown", memo="{{draft.memo}}")) == []

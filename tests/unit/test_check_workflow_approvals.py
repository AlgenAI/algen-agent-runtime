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


def test_reading_other_fields_of_a_modifiable_approval_does_not_count_as_reading_its_decision() -> (
    None
):
    incidental = _workflow(
        modifiable=True, to="{{draft.payment.to}}", note="{{approval.decided_by}}"
    )
    assert _codes(incidental) == ["APPROVAL_MODIFICATION_IGNORED"]


def test_the_checker_uses_the_approvals_that_gate_the_step() -> None:
    """An earlier approval behind the gating one neither covers the step nor is checked."""
    workflow = _workflow(to="{{draft.payment.to}}")["pay"]
    first = WorkflowNode(
        id="first",
        kind=WorkflowNodeKind.APPROVAL,
        output_key="first",
        approval=WorkflowApprovalRule(prompt="Draft?", review_from=("inputs.memo",)),
    )
    nodes = (
        first,
        *(
            n.model_copy(update={"depends_on": ("first",)}) if n.id == "draft" else n
            for n in workflow.nodes
        ),
    )
    gated = {"pay": workflow.model_copy(update={"nodes": nodes})}
    issues = list(
        workflow_approval_issues(
            {"pay": WorkflowManifest.model_validate(gated["pay"].model_dump())}, TOOLS
        )
    )
    assert issues == []
    memo = _workflow(to="{{draft.payment.to}}", memo="{{inputs.memo}}")["pay"]
    nodes = (
        first,
        *(
            n.model_copy(update={"depends_on": ("first",)}) if n.id == "draft" else n
            for n in memo.nodes
        ),
    )
    reported = list(
        workflow_approval_issues(
            {
                "pay": WorkflowManifest.model_validate(
                    memo.model_copy(update={"nodes": nodes}).model_dump()
                )
            },
            TOOLS,
        )
    )
    assert [issue.code for issue in reported] == ["APPROVAL_ARGUMENTS_NOT_REVIEWED"]
    assert "'inputs.memo'" in reported[0].message and "'approve'" in reported[0].message

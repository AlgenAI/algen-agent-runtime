"""Strict binding: a workflow approval covers exactly the tool call the approver was shown (#109)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from conftest import make_runtime

from algen_agent_runtime.orchestration.check import workflow_approval_issues
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolDefinition, canonical_digest
from algen_agent_runtime.workflows import MultiAgentWorkflowExecutor, WorkflowHookRegistry
from algen_agent_runtime.workflows.contracts import (
    WorkflowApprovalRule,
    WorkflowExecutionState,
    WorkflowManifest,
    WorkflowNode,
    WorkflowNodeExecution,
    WorkflowNodeKind,
    WorkflowStatus,
)
from algen_agent_runtime.workflows.service import public_pause


def _runtime(seen: list[dict[str, Any]]) -> Any:
    runtime = make_runtime()
    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="payments.send",
                version="1",
                description="Send a payment",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                side_effect=SideEffect.EXTERNAL,
                requires_approval=True,
            ),
            lambda args, ctx: seen.append(args) or {"sent": True},
        )
    )
    return runtime


def _hooks(**handlers: Any) -> WorkflowHookRegistry:
    hooks = WorkflowHookRegistry()
    for name, handler in handlers.items():
        hooks.register_handler(name, handler)
    return hooks


def _manifest(
    *,
    arguments: dict[str, Any],
    between: bool = False,
    modifiable: bool = True,
    binding: str = "call",
) -> WorkflowManifest:
    """draft -> approve -> [enrich ->] pay"""
    nodes = [
        WorkflowNode(
            id="draft", kind=WorkflowNodeKind.HANDLER, handler="draft", output_key="draft"
        ),
        WorkflowNode(
            id="approve",
            kind=WorkflowNodeKind.APPROVAL,
            depends_on=("draft",),
            output_key="approval",
            approval=WorkflowApprovalRule(
                prompt="Send the payment?",
                parameters_from="draft.payment",
                allow_modification=modifiable,
                binding=binding,
            ),
        ),
    ]
    if between:
        nodes.append(
            WorkflowNode(
                id="enrich",
                kind=WorkflowNodeKind.HANDLER,
                handler="enrich",
                depends_on=("approve",),
                output_key="enriched",
            )
        )
    nodes.append(
        WorkflowNode(
            id="pay",
            kind=WorkflowNodeKind.HANDLER,
            handler="tool:payments.send",
            depends_on=("enrich" if between else "approve",),
            arguments=arguments,
            output_key="paid",
        )
    )
    return WorkflowManifest(name="pay", version="1.0.0", output_key="paid", nodes=tuple(nodes))


PAYMENT = {"to": "acme", "amount": 100}


def _draft(context: Any) -> dict[str, Any]:
    return {"payment": dict(PAYMENT)}


async def _run(
    manifest: WorkflowManifest,
    decision: str = "approved",
    *,
    modified: dict[str, Any] | None = None,
    hooks: WorkflowHookRegistry | None = None,
) -> tuple[WorkflowExecutionState, WorkflowExecutionState, list[dict[str, Any]], list[Any]]:
    seen: list[dict[str, Any]] = []
    events: list[tuple[str, dict[str, Any]]] = []
    executor = MultiAgentWorkflowExecutor(_runtime(seen), hooks or _hooks(draft=_draft))
    paused = await executor.run(manifest, {}, tenant_id="tenant", user_id="user")
    assert paused.status == WorkflowStatus.AWAITING_APPROVAL, paused.error
    done = await executor.decide_approval(
        manifest,
        paused.id,
        tenant_id="tenant",
        user_id="approver",
        decision=decision,
        modified_parameters=modified,
        emit=lambda kind, payload: events.append((kind, payload)),
    )
    return paused, done, seen, events


@pytest.mark.asyncio
async def test_the_approver_is_shown_the_resolved_call_including_literals() -> None:
    manifest = _manifest(
        arguments={"to": "{{approval.parameters.to}}", "amount": 100, "token": "secret"}
    )
    paused, done, seen, events = await _run(manifest)

    [call] = paused.pause["calls"]
    assert call["node_id"] == "pay"
    assert (call["tool"], call["tool_version"]) == ("payments.send", "1")
    # Literals are shown too; sensitive keys are redacted like approval parameters.
    assert call["arguments"] == {"to": "acme", "amount": 100, "token": "[REDACTED]"}
    assert call["arguments_sha256"] == canonical_digest(
        {"to": "acme", "amount": 100, "token": "secret"}
    )
    assert public_pause(paused.pause)["calls"] == paused.pause["calls"]
    assert done.status == WorkflowStatus.COMPLETED, done.error
    assert seen == [{"to": "acme", "amount": 100, "token": "secret"}]
    bound = done.nodes["approve"].approved_calls["pay"]
    assert bound.arguments_sha256 == call["arguments_sha256"]
    authorized = next(data for kind, data in events if kind == "workflow.tool.authorized")
    assert authorized["binding"] == "call"


@pytest.mark.asyncio
async def test_a_modified_decision_binds_the_call_resolved_with_the_modification() -> None:
    manifest = _manifest(arguments={"to": "{{approval.parameters.to}}", "amount": 5})
    _, done, seen, _ = await _run(manifest, "modified", modified={"to": "globex", "amount": 1})

    assert done.status == WorkflowStatus.COMPLETED, done.error
    assert seen == [{"to": "globex", "amount": 5}]


@pytest.mark.asyncio
async def test_arguments_produced_after_the_approval_are_refused() -> None:
    """The bug in #109: a step between the approval and the tool decides the payload."""
    manifest = _manifest(
        arguments={"to": "{{enriched.to}}", "amount": "{{enriched.amount}}"}, between=True
    )
    hooks = _hooks(draft=_draft, enrich=lambda context: {"to": "mallory", "amount": 9999})
    _, done, seen, events = await _run(manifest, hooks=hooks)

    assert done.status == WorkflowStatus.FAILED
    assert seen == []
    assert "approval.arguments_unbound" in (done.nodes["pay"].error or "")
    refused = next(data for kind, data in events if kind == "workflow.tool.refused")
    assert refused["reason_code"] == "approval.arguments_unbound"
    assert refused["approval_node_id"] == "approve"


@pytest.mark.asyncio
async def test_a_value_rewritten_after_the_decision_is_refused() -> None:
    """The shown call read draft.payment; a later step rewrites it before the tool runs."""

    def rewrite(context: Any) -> str:
        context.state.values["draft"]["payment"]["amount"] = 9999
        return "rewritten"

    manifest = _manifest(
        arguments={"to": "{{draft.payment.to}}", "amount": "{{draft.payment.amount}}"},
        between=True,
        modifiable=False,
    )
    _, done, seen, events = await _run(manifest, hooks=_hooks(draft=_draft, enrich=rewrite))

    assert done.status == WorkflowStatus.FAILED
    assert seen == []
    assert "approval.arguments_mismatch" in (done.nodes["pay"].error or "")
    refused = next(data for kind, data in events if kind == "workflow.tool.refused")
    assert refused["arguments_sha256"] == canonical_digest({"to": "acme", "amount": 9999})
    assert refused["approved_arguments_sha256"] == canonical_digest(PAYMENT)


@pytest.mark.asyncio
async def test_a_modification_the_step_does_not_read_is_refused() -> None:
    manifest = _manifest(arguments={"to": "{{draft.payment.to}}", "amount": 100})
    _, done, seen, _ = await _run(manifest, "modified", modified={"to": "globex", "amount": 1})

    assert done.status == WorkflowStatus.FAILED
    assert seen == []
    assert "approval.modification_ignored" in (done.nodes["pay"].error or "")


@pytest.mark.asyncio
async def test_the_same_workflow_approved_unchanged_runs() -> None:
    manifest = _manifest(arguments={"to": "{{draft.payment.to}}", "amount": 100})
    _, done, seen, _ = await _run(manifest)
    assert done.status == WorkflowStatus.COMPLETED, done.error
    assert seen == [{"to": "acme", "amount": 100}]


@pytest.mark.asyncio
async def test_binding_step_is_the_explicit_opt_out() -> None:
    manifest = _manifest(
        arguments={"to": "{{enriched.to}}", "amount": 1}, between=True, binding="step"
    )
    hooks = _hooks(draft=_draft, enrich=lambda context: {"to": "anyone"})
    _, done, seen, events = await _run(manifest, hooks=hooks)

    assert done.status == WorkflowStatus.COMPLETED, done.error
    assert seen == [{"to": "anyone", "amount": 1}]
    authorized = next(data for kind, data in events if kind == "workflow.tool.authorized")
    assert authorized["binding"] == "step"


@pytest.mark.asyncio
async def test_a_call_reading_the_decision_is_shown_unresolved_and_bound_when_decided() -> None:
    manifest = _manifest(
        arguments={"to": "acme", "approver": "{{approval.decided_by}}"}, modifiable=False
    )
    paused, done, seen, _ = await _run(manifest)

    assert paused.pause["calls"][0]["arguments"] is None
    assert done.status == WorkflowStatus.COMPLETED, done.error
    assert seen == [{"to": "acme", "approver": "approver"}]


@pytest.mark.asyncio
async def test_an_approval_decided_before_0_2_runs_its_step_as_before() -> None:
    manifest = _manifest(arguments={"to": "{{draft.payment.to}}", "amount": 1})
    seen: list[dict[str, Any]] = []
    executor = MultiAgentWorkflowExecutor(_runtime(seen), _hooks(draft=_draft))
    paused = await executor.run(manifest, {}, tenant_id="tenant", user_id="user")
    done = await executor.decide_approval(
        manifest, paused.id, tenant_id="tenant", user_id="approver", decision="approved"
    )
    assert done.status == WorkflowStatus.COMPLETED

    # A checkpoint written before 0.2: the approval node has no approved_calls.
    legacy = WorkflowNodeExecution(node_id="approve")
    assert "approved_calls" not in legacy.model_dump(mode="json")
    nodes = {node.id: node for node in manifest.nodes}
    done.nodes["approve"] = legacy
    assert executor._binding_problem(done, nodes, "approve", nodes["pay"], "0" * 64) is None
    assert executor._binding_of(done, nodes, ("approve",)) == "legacy"


def test_the_manifest_fingerprint_ignores_the_default_binding() -> None:
    rule = WorkflowApprovalRule(prompt="Send?")
    assert "binding" not in rule.model_dump(mode="json")
    assert WorkflowApprovalRule(prompt="Send?", binding="step").model_dump()["binding"] == "step"


TOOLS = (SimpleNamespace(name="payments.send", requires_approval=True),)


def _codes(manifest: WorkflowManifest) -> dict[str, str]:
    return {
        issue.code: issue.severity
        for issue in workflow_approval_issues({manifest.name: manifest}, TOOLS)
    }


def test_check_reports_arguments_produced_after_the_approval_as_an_error() -> None:
    manifest = _manifest(arguments={"to": "{{enriched.to}}"}, between=True, modifiable=False)
    assert _codes(manifest)["APPROVAL_ARGUMENTS_UNBOUND"] == "error"
    assert "APPROVAL_ARGUMENTS_UNBOUND" not in _codes(
        _manifest(arguments={"to": "{{approval.parameters.to}}"}, between=True)
    )


def test_check_warns_about_binding_step() -> None:
    manifest = _manifest(arguments={"to": "{{enriched.to}}"}, between=True, binding="step")
    codes = _codes(manifest)
    assert codes["APPROVAL_BINDING_STEP"] == "warning"
    assert "APPROVAL_ARGUMENTS_UNBOUND" not in codes

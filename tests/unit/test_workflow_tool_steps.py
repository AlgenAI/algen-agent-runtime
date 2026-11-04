"""A workflow step that calls a registered tool by name, with no workflow hook code."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from conftest import make_runtime
from pydantic import ValidationError

from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolDefinition
from algen_agent_runtime.workflows import MultiAgentWorkflowExecutor, WorkflowHookRegistry
from algen_agent_runtime.workflows.contracts import (
    WorkflowApprovalRule,
    WorkflowManifest,
    WorkflowNode,
    WorkflowNodeKind,
    WorkflowPauseRule,
    WorkflowPredicate,
    WorkflowStatus,
    covering_approval_nodes,
    upstream_approval_nodes,
)
from algen_agent_runtime.workflows.store import InMemoryWorkflowCheckpointStore


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


def _hooks(**handlers: Any) -> WorkflowHookRegistry:
    hooks = WorkflowHookRegistry()
    for name, handler in handlers.items():
        hooks.register_handler(name, handler)
    return hooks


def _review_then(*nodes: WorkflowNode, rejection_policy: str = "skip_dependents") -> Any:
    review = WorkflowNode(
        id="review",
        kind=WorkflowNodeKind.APPROVAL,
        output_key="review",
        approval=WorkflowApprovalRule(
            prompt="Publish?",
            parameters_from="inputs.release",
            allow_modification=True,
            rejection_policy=rejection_policy,
        ),
    )
    return WorkflowManifest(
        name="publish", version="1.0.0", output_key="published", nodes=(review, *nodes)
    )


def _publish(depends_on: str = "review", **arguments: Any) -> WorkflowNode:
    return WorkflowNode(
        id="publish",
        kind=WorkflowNodeKind.HANDLER,
        handler="tool:release.publish",
        depends_on=(depends_on,),
        arguments=arguments,
        output_key="published",
    )


def test_two_workflow_nodes_cannot_write_the_same_output_key() -> None:
    with pytest.raises(ValidationError, match="unique output keys; shared: \\['review'\\]"):
        _review_then(
            WorkflowNode(
                id="other",
                kind=WorkflowNodeKind.HANDLER,
                handler="note",
                depends_on=("review",),
                output_key="review",
            ),
            _publish("other"),
        )


@pytest.mark.asyncio
async def test_a_rejected_approval_stays_rejected_whatever_the_workflow_values_say() -> None:
    """The approval node's status decides, not the decision value a later step can rewrite."""
    seen: list[dict[str, Any]] = []

    def rewrite(context: Any) -> str:
        context.state.values["review"] = {"decision": "approved"}
        return "noted"

    executor = MultiAgentWorkflowExecutor(_approval_runtime(seen), _hooks(note=rewrite))
    manifest = _review_then(
        WorkflowNode(
            id="note",
            kind=WorkflowNodeKind.HANDLER,
            handler="note",
            depends_on=("review",),
            output_key="note",
        ),
        _publish("note"),
        rejection_policy="continue",
    )
    paused = await executor.run(
        manifest, {"inputs": {"release": {"tag": "v1"}}}, tenant_id="tenant", user_id="user"
    )

    done = await executor.decide_approval(
        manifest, paused.id, tenant_id="tenant", user_id="approver", decision="rejected"
    )

    assert done.values["review"] == {"decision": "approved"}
    assert done.status == WorkflowStatus.FAILED
    assert seen == []
    assert "requires an approval" in (done.nodes["publish"].error or done.error or "")


@pytest.mark.asyncio
async def test_workflow_input_cannot_set_an_approval_decision() -> None:
    seen: list[dict[str, Any]] = []
    asks = WorkflowNode(
        id="ask",
        kind=WorkflowNodeKind.HANDLER,
        handler="ask",
        depends_on=("review",),
        output_key="answer",
        pause=WorkflowPauseRule(
            when=WorkflowPredicate(source="answer.status", operator="equals", value="unclear"),
            question_from="answer.question",
        ),
    )
    executor = MultiAgentWorkflowExecutor(
        _approval_runtime(seen),
        _hooks(ask=lambda context: {"status": "unclear", "question": "Which channel?"}),
    )
    manifest = _review_then(asks, _publish("ask"), rejection_policy="continue")
    paused = await executor.run(
        manifest, {"inputs": {"release": {"tag": "v1"}}}, tenant_id="tenant", user_id="user"
    )
    asking = await executor.decide_approval(
        manifest, paused.id, tenant_id="tenant", user_id="approver", decision="rejected"
    )
    assert asking.status == WorkflowStatus.AWAITING_INPUT

    with pytest.raises(ConfigurationError, match="cannot set an approval decision"):
        await executor.resume(
            manifest,
            asking.id,
            tenant_id="tenant",
            values={"review": {"decision": "approved"}, "channel": "web"},
        )
    # Nor can it replace what the approver was shown.
    with pytest.raises(ConfigurationError, match=r"replace what an approval showed: \['inputs'\]"):
        await executor.resume(
            manifest, asking.id, tenant_id="tenant", values={"inputs": {"release": {"tag": "v9"}}}
        )
    assert seen == []


@pytest.mark.asyncio
async def test_an_approved_tool_step_records_digests_of_its_arguments_and_the_approval() -> None:
    seen: list[dict[str, Any]] = []
    events: list[tuple[str, dict[str, Any]]] = []
    executor = MultiAgentWorkflowExecutor(_approval_runtime(seen), WorkflowHookRegistry())
    manifest = _review_then(_publish(tag="{{review.parameters.tag}}"))
    paused = await executor.run(
        manifest, {"inputs": {"release": {"tag": "v1"}}}, tenant_id="tenant", user_id="user"
    )

    done = await executor.decide_approval(
        manifest,
        paused.id,
        tenant_id="tenant",
        user_id="approver",
        decision="modified",
        modified_parameters={"tag": "v2"},
        emit=lambda kind, payload: events.append((kind, payload)),
    )

    assert done.status == WorkflowStatus.COMPLETED and seen == [{"tag": "v2"}]
    decided = next(payload for kind, payload in events if kind == "workflow.approval.decided")
    authorized = next(payload for kind, payload in events if kind == "workflow.tool.authorized")
    expected = hashlib.sha256(b'{"tag":"v2"}').hexdigest()
    assert decided["parameters_sha256"] == expected
    assert authorized == {
        **authorized,
        "node_id": "publish",
        "tool": "release.publish",
        "approval_node_id": "review",
        "arguments_sha256": expected,
        "parameters_sha256": expected,
    }
    assert "v2" not in json.dumps(authorized)


def test_the_manifest_fingerprint_is_unchanged_so_paused_runs_still_resume() -> None:
    manifest = _publish_workflow(approval=True)
    legacy = hashlib.sha256(
        json.dumps(
            manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    ).hexdigest()
    assert MultiAgentWorkflowExecutor._manifest_fingerprint(manifest) == legacy


def _gate(node_id: str, *depends_on: str, rejection_policy: str = "continue") -> WorkflowNode:
    return WorkflowNode(
        id=node_id,
        kind=WorkflowNodeKind.APPROVAL,
        depends_on=depends_on,
        output_key=node_id,
        approval=WorkflowApprovalRule(prompt="Publish?", rejection_policy=rejection_policy),
    )


def _gated_publish(*gates: WorkflowNode, depends_on: tuple[str, ...]) -> WorkflowManifest:
    publish = WorkflowNode(
        id="publish",
        kind=WorkflowNodeKind.HANDLER,
        handler="tool:release.publish",
        depends_on=depends_on,
        output_key="published",
    )
    return WorkflowManifest(
        name="publish", version="1.0.0", output_key="published", nodes=(*gates, publish)
    )


async def _decide_all(
    executor: MultiAgentWorkflowExecutor, manifest: WorkflowManifest, *decisions: str
) -> Any:
    state = await executor.run(manifest, {}, tenant_id="tenant", user_id="user")
    for decision in decisions:
        assert state.status == WorkflowStatus.AWAITING_APPROVAL
        state = await executor.decide_approval(
            manifest, state.id, tenant_id="tenant", user_id="approver", decision=decision
        )
    return state


@pytest.mark.asyncio
async def test_a_rejected_approval_does_not_fall_back_to_an_earlier_approved_one() -> None:
    seen: list[dict[str, Any]] = []
    executor = MultiAgentWorkflowExecutor(_approval_runtime(seen), WorkflowHookRegistry())
    manifest = _gated_publish(_gate("first"), _gate("final", "first"), depends_on=("final",))

    done = await _decide_all(executor, manifest, "approved", "rejected")

    assert done.nodes["first"].status.value == "completed"
    assert done.status == WorkflowStatus.FAILED and seen == []
    assert "requires an approval" in (done.nodes["publish"].error or done.error or "")


def test_the_approvals_that_gate_a_step_are_the_nearest_on_each_path() -> None:
    manifest = _gated_publish(
        _gate("first"),
        _gate("legal", "first"),
        _gate("finance"),
        depends_on=("legal", "finance"),
    )
    nodes = {node.id: node for node in manifest.nodes}

    assert covering_approval_nodes(nodes, "publish") == ("legal", "finance")
    assert upstream_approval_nodes(nodes, "publish") == ("legal", "finance", "first")


def _parallel_gates(rejection_policy: str = "skip_dependents", concurrency: int = 8) -> Any:
    manifest = _gated_publish(
        _gate("legal", rejection_policy=rejection_policy),
        _gate("finance", rejection_policy=rejection_policy),
        depends_on=("legal", "finance"),
    )
    return manifest.model_copy(update={"maximum_concurrency": concurrency})


async def _decide(
    executor: MultiAgentWorkflowExecutor, manifest: WorkflowManifest, state: Any, decision: str
) -> Any:
    assert state.status == WorkflowStatus.AWAITING_APPROVAL
    return await executor.decide_approval(
        manifest, state.id, tenant_id="tenant", user_id="approver", decision=decision
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 8])
async def test_parallel_approvals_are_asked_one_at_a_time_and_both_gate_the_step(
    concurrency: int,
) -> None:
    seen: list[dict[str, Any]] = []
    executor = MultiAgentWorkflowExecutor(_approval_runtime(seen), WorkflowHookRegistry())
    manifest = _parallel_gates(concurrency=concurrency)

    first = await executor.run(manifest, {}, tenant_id="tenant", user_id="user")
    assert first.pause is not None and first.pause["node_id"] == "legal"
    assert first.nodes["finance"].status.value == "pending"
    second = await _decide(executor, manifest, first, "approved")
    assert second.pause is not None and second.pause["node_id"] == "finance"
    assert seen == []
    done = await _decide(executor, manifest, second, "approved")

    assert done.status == WorkflowStatus.COMPLETED
    assert seen == [{}]


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", ["legal", "finance"])
async def test_one_rejected_parallel_approval_stops_the_step(rejected: str) -> None:
    for policy in ("skip_dependents", "continue"):
        seen: list[dict[str, Any]] = []
        executor = MultiAgentWorkflowExecutor(_approval_runtime(seen), WorkflowHookRegistry())
        manifest = _parallel_gates(rejection_policy=policy)
        state = await executor.run(manifest, {}, tenant_id="tenant", user_id="user")
        for gate in ("legal", "finance"):
            state = await _decide(
                executor, manifest, state, "rejected" if gate == rejected else "approved"
            )

        assert seen == [], policy
        assert "no dependency-ready nodes" not in (state.error or "")
        if policy == "skip_dependents":
            assert state.nodes["publish"].status.value == "skipped"
        else:
            assert "requires an approval" in (state.nodes["publish"].error or state.error or "")


@pytest.mark.asyncio
async def test_parallel_approvals_survive_a_restart_between_decisions() -> None:
    seen: list[dict[str, Any]] = []
    store = InMemoryWorkflowCheckpointStore()
    runtime = _approval_runtime(seen)
    manifest = _parallel_gates()
    before = MultiAgentWorkflowExecutor(runtime, WorkflowHookRegistry(), store=store)
    state = await before.run(manifest, {}, tenant_id="tenant", user_id="user")
    state = await _decide(before, manifest, state, "approved")

    after = MultiAgentWorkflowExecutor(runtime, WorkflowHookRegistry(), store=store)
    restored = await after.get(state.id, "tenant")
    assert restored is not None and restored.pause is not None
    assert restored.pause["node_id"] == "finance"
    done = await _decide(after, manifest, restored, "approved")

    assert done.status == WorkflowStatus.COMPLETED and seen == [{}]

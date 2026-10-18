from __future__ import annotations

import pytest
from conftest import make_agent, make_runtime
from test_context_compaction import agent, setup_runtime, state

from algen_agent_runtime.context.compaction import assess_compaction, plan_compaction
from algen_agent_runtime.context.diagnostics import CompactionDiagnostic, context_snapshot
from algen_agent_runtime.context.packing import (
    CONTEXT_SUMMARY_NAME,
    WORKING_STATE_NAME,
    estimate_message,
)
from algen_agent_runtime.exceptions.errors import ToolExecutionError
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.types.contracts import ContextPolicy, Message, Role, RunRequest, ToolCall


@pytest.mark.parametrize(
    "reason",
    [
        "disabled",
        "history_uninitialized",
        "pending_tools",
        "below_trigger",
        "recent_groups_only",
        "incomplete_tool_group",
        "input_limit",
        "no_source",
        "ready",
    ],
)
def test_compaction_reason_matches_selected_plan_without_exposing_content(reason) -> None:
    current = state()
    policy = agent().context_policy
    if reason == "disabled":
        policy = ContextPolicy()
    elif reason == "history_uninitialized":
        current.context_history = None
    elif reason == "pending_tools":
        current.pending_tool_calls = [ToolCall(name="lookup")]
    elif reason == "below_trigger":
        policy = policy.model_copy(update={"compaction_trigger_tokens": 12000})
    elif reason == "recent_groups_only":
        policy = policy.model_copy(update={"compaction_keep_recent_groups": 10})
    elif reason == "incomplete_tool_group":
        current.context_history.insert(
            1, Message(role=Role.ASSISTANT, content=(), tool_calls=(ToolCall(name="lookup"),))
        )
    elif reason == "input_limit":
        policy = policy.model_copy(update={"compaction_max_input_tokens": 256})
    elif reason == "no_source":
        current.context_history = [
            Message.text(Role.SYSTEM, "Private instructions " * 150),
            *current.context_history[-2:],
        ]
    assessment = assess_compaction(current, policy)
    assert assessment.diagnostic.reason == reason
    assert assessment.plan == plan_compaction(current, policy)
    assert bool(assessment.plan) == (reason == "ready")
    serialized = assessment.diagnostic.model_dump_json()
    assert "Original goal" not in serialized
    assert "Private instructions" not in serialized
    assert CompactionDiagnostic.model_validate_json(serialized) == assessment.diagnostic


def test_snapshot_reports_estimates_and_distinguishes_missing_history() -> None:
    current = state()
    current.messages = [
        Message.text(Role.USER, "Private user content"),
        Message.text(Role.ASSISTANT, "Private summary").model_copy(
            update={"name": CONTEXT_SUMMARY_NAME}
        ),
        Message.text(Role.ASSISTANT, "Private working state").model_copy(
            update={"name": WORKING_STATE_NAME}
        ),
    ]
    snapshot = context_snapshot(current)
    assert snapshot.estimated_tokens == sum(map(estimate_message, current.messages))
    assert snapshot.summary_tokens == estimate_message(current.messages[1])
    assert snapshot.working_state_tokens == estimate_message(current.messages[2])
    assert snapshot.retained_history_messages == 5
    assert snapshot.archived_history_messages == 0
    assert "Private" not in snapshot.model_dump_json()
    current.context_history = None
    assert context_snapshot(current).retained_history_messages is None


async def test_simple_runs_publish_context_estimates_without_compaction_events() -> None:
    runtime = make_runtime(agent=make_agent())
    result = await runtime.run(
        RunRequest(agent="test-agent", input="Private question", tenant_id="tenant", user_id="user")
    )
    events = await runtime.events.history(result.run_id)
    event = next(event for event in events if event.type == "context.retrieved")
    assert event.data["estimated_tokens"] > 0
    assert event.data["retained_history_messages"] == 2
    assert "Private question" not in str(event.data)
    assert not any(event.type == "context.compaction.assessed" for event in events)


async def test_compaction_diagnostics_order_and_reduction() -> None:
    runtime, current, _, _ = await setup_runtime()
    await runtime._compact_context(current, agent())
    events = [
        event
        for event in await runtime.events.history(current.id)
        if event.type.startswith("context.")
    ]
    assert [event.type for event in events] == ["context.compaction.assessed", "context.compacted"]
    assert events[0].data["reason"] == "ready"
    assert events[1].data["history_tokens_after"] < events[1].data["history_tokens_before"]
    assert "Original goal" not in str([event.data for event in events])
    await runtime._compact_context(current, agent())
    events = await runtime.events.history(current.id)
    assert events[-1].type == "context.compaction.assessed"
    assert events[-1].data["reason"] in {"below_trigger", "recent_groups_only"}


async def test_ready_assessment_is_not_a_commit_receipt_when_archiving_fails() -> None:
    runtime, current, _, _ = await setup_runtime(store=InMemoryArtifactStore(max_bytes=100))
    with pytest.raises(ToolExecutionError):
        await runtime._compact_context(current, agent())
    events = await runtime.events.history(current.id)
    assert any(
        event.type == "context.compaction.assessed" and event.data["reason"] == "ready"
        for event in events
    )
    assert not any(event.type == "context.compacted" for event in events)
    assert (await runtime.status(current.id, "tenant")).context_compaction is None


async def test_pending_tool_diagnostic_does_not_dispatch_summary() -> None:
    runtime, current, _, provider = await setup_runtime()
    current.pending_tool_calls = [ToolCall(name="lookup")]
    await runtime._compact_context(current, agent())
    assert not provider.requests
    assert (await runtime.events.history(current.id))[-1].data["reason"] == "pending_tools"

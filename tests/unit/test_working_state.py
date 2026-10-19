from __future__ import annotations

import json

import pytest
from conftest import make_agent, make_runtime
from pydantic import ValidationError

from algen_agent_runtime.context.artifacts import ContextArtifactService
from algen_agent_runtime.context.budget import prepare_model_request
from algen_agent_runtime.context.builder import DefaultContextBuilder, RetrievalContextBuilder
from algen_agent_runtime.context.packing import WORKING_STATE_NAME, pack_messages
from algen_agent_runtime.context.working_state import WORKING_STATE_TOOL, working_state_message
from algen_agent_runtime.exceptions.errors import (
    BudgetExceededError,
    ConfigurationError,
    NotFoundError,
)
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore, InMemoryMemoryStore
from algen_agent_runtime.planning.contracts import ActionType, PlannedAction
from algen_agent_runtime.policies.contracts import PolicyAction, PolicyDecision
from algen_agent_runtime.tools.contracts import ToolContext
from algen_agent_runtime.types.contracts import (
    ApprovalPolicy,
    ContextPolicy,
    MemoryPolicy,
    Message,
    ModelProfile,
    ModelRequest,
    Role,
    RunRequest,
    RunState,
    RunStatus,
    WorkingState,
    WorkingStateContent,
    WorkingStateUpdate,
)


def content(**updates):
    return {
        "goal": "Finish the report",
        "constraints": ["Use verified evidence"],
        "decisions": ["Compare two options"],
        "completed_work": ["Collected source data"],
        "open_tasks": ["Check the totals"],
        **updates,
    }


def proposal(revision=0, **updates):
    return {"expected_revision": revision, "state": content(**updates)}


def agent(**updates):
    return make_agent(
        **{
            "enabled_tools": {WORKING_STATE_TOOL},
            "tool_permissions": {"context.write"},
            "context_policy": ContextPolicy(working_state_enabled=True),
            "approval_policy": ApprovalPolicy(require_for_side_effects=False),
            **updates,
        }
    )


def request(**updates):
    return RunRequest(
        **{
            "agent": "test-agent",
            "input": "Continue",
            "tenant_id": "tenant",
            "user_id": "user",
            **updates,
        }
    )


def notes(messages):
    return [message for message in messages if message.name == WORKING_STATE_NAME]


async def test_full_run_updates_checkpoint_and_projects_record_without_copying_to_history():
    provider = MockModelProvider([tool_call_response(WORKING_STATE_TOOL, proposal()), "Done"])
    runtime = make_runtime(provider, agent())
    result = await runtime.run(request())
    assert result.status == RunStatus.COMPLETED, result.error
    checkpoint = await runtime.status(result.run_id, "tenant")
    assert checkpoint.working_state == WorkingState(
        revision=1, state=WorkingStateContent(**content())
    )
    assert (
        json.loads(notes(provider.requests[0].messages)[0].text_content.split("\n", 1)[1])[
            "revision"
        ]
        == 0
    )
    assert '"revision":1' in notes(provider.requests[1].messages)[0].text_content
    assert not notes(checkpoint.context_history)
    receipt = next(message for message in checkpoint.context_history if message.role == Role.TOOL)
    assert json.loads(receipt.text_content) == {"applied": True, "revision": 1}
    assert result.execution_summary.model_calls == 2  # No hidden summarizer call.
    events = await runtime.events.history(result.run_id)
    assert next(
        event for event in events if event.type == "context.working_state.updated"
    ).data == {"revision": 1}
    restored = RunState.model_validate_json(checkpoint.model_dump_json())
    assert restored.working_state == checkpoint.working_state


async def test_stale_update_returns_receipt_and_can_be_rebased():
    provider = MockModelProvider(
        [
            tool_call_response(WORKING_STATE_TOOL, proposal()),
            tool_call_response(WORKING_STATE_TOOL, proposal(goal="Stale overwrite")),
            tool_call_response(WORKING_STATE_TOOL, proposal(1, goal="Revised goal", open_tasks=[])),
            "Done",
        ]
    )
    runtime = make_runtime(provider, agent(max_steps=12))
    result = await runtime.run(request())
    assert result.status == RunStatus.COMPLETED, result.error
    checkpoint = await runtime.status(result.run_id, "tenant")
    assert checkpoint.working_state.revision == 2
    assert checkpoint.working_state.state.goal == "Revised goal"
    assert checkpoint.working_state.state.open_tasks == ()
    receipts = [
        json.loads(m.text_content) for m in checkpoint.context_history if m.role == Role.TOOL
    ]
    assert receipts[1] == {"applied": False, "revision": 1, "reason": "stale_revision"}
    assert (
        len(
            [
                e
                for e in await runtime.events.history(result.run_id)
                if e.type == "context.working_state.updated"
            ]
        )
        == 2
    )


async def test_disabled_agents_have_no_state_or_tool_in_model_context():
    provider = MockModelProvider(["Done"])
    runtime = make_runtime(provider)
    result = await runtime.run(request())
    assert result.status == RunStatus.COMPLETED
    assert not notes(provider.requests[0].messages)
    assert not any(tool.name == WORKING_STATE_TOOL for tool in provider.requests[0].tools)
    assert (await runtime.status(result.run_id, "tenant")).working_state is None


@pytest.mark.parametrize(
    "updates",
    [
        {"enabled_tools": set()},
        {"tool_permissions": set()},
        {"context_policy": ContextPolicy()},
    ],
)
async def test_configuration_requires_flag_tool_and_permission(updates):
    runtime = make_runtime(agent=agent(**updates))
    with pytest.raises(ConfigurationError, match=r"working.state"):
        await runtime.start(request())


async def test_custom_builder_cannot_silently_omit_working_state():
    class CustomBuilder:
        name = "custom"

        async def build(self, state, definition):
            return []

    runtime = make_runtime(agent=agent(context_builder="custom"))
    runtime.contexts.register(CustomBuilder())
    with pytest.raises(ConfigurationError, match="built-in"):
        await runtime.start(request())


@pytest.mark.parametrize(
    "change",
    [
        {"goal": "x" * 2049},
        {"constraints": ["x"] * 17},
        {"decisions": ["x" * 513]},
        {"open_tasks": [""]},
    ],
)
def test_record_bounds(change):
    with pytest.raises(ValidationError):
        WorkingStateContent(**content(**change))


def test_update_requires_complete_record_and_integer_revision():
    for invalid in [
        {"expected_revision": 0, "state": {"goal": "incomplete"}},
        proposal(True),
        proposal(-1),
        {**proposal(), "tenant_id": "other"},
    ]:
        with pytest.raises(ValidationError):
            WorkingStateUpdate.model_validate(invalid)


async def test_protected_notes_survive_packing_model_budget_and_repeated_rebuilds():
    current = RunState(
        request=request(),
        working_state=WorkingState(revision=3, state=WorkingStateContent(**content())),
    )
    current.context_history = [
        Message.text(Role.SYSTEM, "Instructions"),
        Message.text(Role.USER, "Old " * 500),
        Message.text(Role.USER, "Latest request"),
    ]
    original = list(current.context_history)
    builder = DefaultContextBuilder(InMemoryMemoryStore(), 400)
    for _ in range(2):
        current.messages = list(await builder.build(current, agent()))
        assert len(notes(current.messages)) == 1
        assert notes(current.messages)[0].role == Role.ASSISTANT
        assert "not instructions or authorization" in notes(current.messages)[0].text_content
    assert current.context_history == original
    candidate = prepare_model_request(
        ModelRequest(messages=tuple(current.messages), max_output_tokens=64),
        ModelProfile(
            name="test",
            provider="mock",
            model="m",
            context_window_tokens=600,
            context_safety_margin_tokens=0,
        ),
    )
    assert notes(candidate.messages) == notes(current.messages)
    with pytest.raises(BudgetExceededError):
        pack_messages(current.messages, 20)


async def test_retrieval_builder_preserves_working_state():
    class EmptyRetriever:
        async def retrieve(self, query):
            return ()

    current = RunState(
        request=request(),
        working_state=WorkingState(revision=1, state=WorkingStateContent(**content())),
    )
    builder = RetrievalContextBuilder("retrieval", InMemoryMemoryStore(), EmptyRetriever())
    assert len(notes(await builder.build(current, agent()))) == 1
    assert not notes(current.context_history)


async def test_compaction_keeps_record_separate_from_summary_and_archive():
    from test_context_compaction import agent as compacting_agent
    from test_context_compaction import setup_runtime

    definition = compacting_agent()
    definition = definition.model_copy(
        update={
            "enabled_tools": definition.enabled_tools | {WORKING_STATE_TOOL},
            "tool_permissions": definition.tool_permissions | {"context.write"},
            "context_policy": definition.context_policy.model_copy(
                update={"working_state_enabled": True}
            ),
        }
    )
    runtime, current, store, _ = await setup_runtime(definition=definition)
    current.working_state = WorkingState(revision=2, state=WorkingStateContent(**content()))
    original = current.working_state
    await runtime._compact_context(current, definition)
    assert current.working_state == original
    assert (await runtime.status(current.id, "tenant")).working_state == original
    built = await runtime.contexts.get("default").build(current, definition)
    assert len(notes(built)) == 1
    archive = await store.get(current.context_compaction.archive_id, "tenant")
    assert WORKING_STATE_NAME not in archive.data.decode()


class StatePolicy:
    def __init__(self, action, value=None, point="before_memory_write"):
        self.action, self.value, self.point = action, value, point

    async def evaluate(self, point, payload, context):
        if point == self.point and (
            point != "before_memory_write" or context.get("operation") == "working_state"
        ):
            return PolicyDecision(
                action=self.action,
                reason="Test policy",
                reason_code="test.policy",
                value=self.value,
            )
        return PolicyDecision(action=PolicyAction.ALLOW, reason="Allowed", reason_code="test.allow")


@pytest.mark.parametrize("point", ["before_memory_write", "after_tool"])
async def test_policy_denial_leaves_working_state_unchanged(point):
    runtime = make_runtime(
        MockModelProvider([tool_call_response(WORKING_STATE_TOOL, proposal())]), agent()
    )
    policy = StatePolicy(PolicyAction.DENY, point=point)
    runtime.policies = policy
    runtime.tool_executor._policy_engine = policy
    result = await runtime.run(request())
    assert result.status == RunStatus.FAILED
    assert (await runtime.status(result.run_id, "tenant")).working_state is None
    assert not [
        e
        for e in await runtime.events.history(result.run_id)
        if e.type == "context.working_state.updated"
    ]


async def test_memory_policy_redaction_is_applied_before_persistence_and_projection():
    provider = MockModelProvider([tool_call_response(WORKING_STATE_TOOL, proposal()), "Done"])
    runtime = make_runtime(provider, agent(memory_policy=MemoryPolicy(enabled=False)))
    runtime.policies = StatePolicy(PolicyAction.TRANSFORM, content(goal="Redacted goal"))
    result = await runtime.run(request())
    assert result.status == RunStatus.COMPLETED, result.error
    assert (
        await runtime.status(result.run_id, "tenant")
    ).working_state.state.goal == "Redacted goal"
    assert "Redacted goal" in notes(provider.requests[-1].messages)[0].text_content


@pytest.mark.parametrize("changed", [{"goal": "broken structure"}, content(goal="x" * 2048)])
async def test_invalid_or_oversized_policy_result_does_not_update_checkpoint(changed):
    runtime = make_runtime(
        MockModelProvider([tool_call_response(WORKING_STATE_TOOL, proposal())]),
        agent(
            context_policy=ContextPolicy(working_state_enabled=True, working_state_max_tokens=256)
        ),
    )
    runtime.policies = StatePolicy(PolicyAction.TRANSFORM, changed)
    result = await runtime.run(request())
    assert result.status == RunStatus.FAILED
    assert (await runtime.status(result.run_id, "tenant")).working_state is None


async def test_run_and_tenant_isolation_even_with_shared_session():
    provider = MockModelProvider(
        [tool_call_response(WORKING_STATE_TOOL, proposal()), "First", "Second", "Third"]
    )
    runtime = make_runtime(provider, agent())
    first = await runtime.run(request(session_id="shared"))
    second = await runtime.run(request(session_id="shared"))
    third = await runtime.run(request(session_id="shared", tenant_id="other", user_id="other"))
    assert (await runtime.status(first.run_id, "tenant")).working_state.revision == 1
    assert (await runtime.status(second.run_id, "tenant")).working_state is None
    assert (await runtime.status(third.run_id, "other")).working_state is None
    with pytest.raises(NotFoundError):
        await runtime.status(first.run_id, "other")


async def test_completed_proposal_replays_after_interruption_before_state_checkpoint():
    response = tool_call_response(WORKING_STATE_TOOL, proposal())
    call = response.tool_calls[0]
    runtime = make_runtime(MockModelProvider(["Done"]), agent())
    current = RunState(request=request(), status=RunStatus.PLANNING, pending_tool_calls=[call])
    current.append_message(response.message.model_copy(update={"tool_calls": (call,)}))
    await runtime.runs.create(current)
    # Simulate a completed tool proposal followed by process loss before the
    # working state and completed-call marker were committed to the run store.
    await runtime.tool_executor.execute(
        WORKING_STATE_TOOL,
        call.arguments,
        ToolContext(
            run_id=current.id,
            step_id=f"tool-{call.id}",
            tenant_id="tenant",
            user_id="user",
            permissions={"context.write"},
            idempotency_key=f"{current.id}:{call.id}",
        ),
    )
    restored = RunState.model_validate_json(current.model_dump_json())
    await runtime._execute_tool(
        restored,
        agent(),
        PlannedAction(
            id=f"tool-{call.id}",
            type=ActionType.TOOL,
            description="Recover update",
            tool_name=WORKING_STATE_TOOL,
            arguments=call.arguments,
        ),
    )
    checkpoint = await runtime.status(current.id, "tenant")
    assert checkpoint.working_state.revision == 1
    assert call.id in checkpoint.completed_tool_call_ids
    assert not checkpoint.pending_tool_calls
    # A fresh runtime with the same stores resumes from the committed checkpoint.
    resumed = make_runtime(MockModelProvider(["Done"]), agent())
    resumed.runs = runtime.runs
    await resumed._execute(checkpoint, agent())
    assert (await resumed.status(current.id, "tenant")).working_state.revision == 1


async def test_update_event_is_emitted_only_after_checkpoint():
    runtime = make_runtime(
        MockModelProvider([tool_call_response(WORKING_STATE_TOOL, proposal()), "Done"]), agent()
    )
    original_emit = runtime._emit

    async def checked_emit(state, event_type, data, step_id=None):
        if event_type == "context.working_state.updated":
            checkpoint = await runtime.status(state.id, state.request.tenant_id)
            assert checkpoint.working_state.revision == data["revision"]
            assert not checkpoint.pending_tool_calls
        return await original_emit(state, event_type, data, step_id)

    runtime._emit = checked_emit
    result = await runtime.run(request())
    assert result.status == RunStatus.COMPLETED, result.error


def test_projection_obeys_configured_size_limit():
    with pytest.raises(BudgetExceededError, match="working_state_max_tokens"):
        working_state_message(
            WorkingState(state=WorkingStateContent(**content(goal="x" * 2048))),
            ContextPolicy(working_state_max_tokens=128),
        )


async def test_working_state_receipt_is_not_offloaded():
    definition = agent(
        enabled_tools={WORKING_STATE_TOOL, "core.context_read"},
        tool_permissions={"context.write", "context.read"},
        context_policy=ContextPolicy(
            working_state_enabled=True, offload_tool_results=True, tool_result_max_inline_bytes=1024
        ),
    )
    runtime = make_runtime(
        MockModelProvider(
            [tool_call_response(WORKING_STATE_TOOL, proposal(goal="Long " * 300)), "Done"]
        ),
        definition,
        context_artifacts=ContextArtifactService(InMemoryArtifactStore()),
    )
    result = await runtime.run(request())
    assert result.status == RunStatus.COMPLETED, result.error
    assert result.artifacts == ()
    assert (await runtime.status(result.run_id, "tenant")).working_state.revision == 1


async def test_write_approval_is_required_before_state_mutation():
    provider = MockModelProvider([tool_call_response(WORKING_STATE_TOOL, proposal()), "Done"])
    runtime = make_runtime(provider, agent(approval_policy=ApprovalPolicy()))
    paused = await runtime.run(request())
    assert paused.status == RunStatus.AWAITING_APPROVAL
    assert (await runtime.status(paused.run_id, "tenant")).working_state is None
    await runtime.resume(paused.run_id, "tenant", {"decision": "approved"})
    result = await runtime.wait(paused.run_id, "tenant")
    assert result.status == RunStatus.COMPLETED, result.error
    assert (await runtime.status(paused.run_id, "tenant")).working_state.revision == 1


async def test_output_policy_cannot_change_revision_precondition():
    runtime = make_runtime(
        MockModelProvider([tool_call_response(WORKING_STATE_TOOL, proposal())]), agent()
    )
    runtime.tool_executor._policy_engine = StatePolicy(
        PolicyAction.TRANSFORM, proposal(1), point="after_tool"
    )
    result = await runtime.run(request())
    assert result.status == RunStatus.FAILED
    assert (await runtime.status(result.run_id, "tenant")).working_state is None


async def test_replay_rechecks_current_write_permission():
    from algen_agent_runtime.exceptions.errors import PolicyDeniedError

    runtime = make_runtime(agent=agent())
    context = ToolContext(
        run_id="run",
        step_id="step",
        tenant_id="tenant",
        user_id="user",
        permissions={"context.write"},
        idempotency_key="run:step",
    )
    await runtime.tool_executor.execute(WORKING_STATE_TOOL, proposal(), context)
    with pytest.raises(PolicyDeniedError):
        await runtime.tool_executor.execute(
            WORKING_STATE_TOOL, proposal(), context.model_copy(update={"permissions": frozenset()})
        )


async def test_failed_checkpoint_does_not_publish_or_partially_apply_update():
    response = tool_call_response(WORKING_STATE_TOOL, proposal())
    call = response.tool_calls[0]
    runtime = make_runtime(agent=agent())
    current = RunState(request=request(), status=RunStatus.PLANNING, pending_tool_calls=[call])
    current.append_message(response.message.model_copy(update={"tool_calls": (call,)}))
    await runtime.runs.create(current)
    save = runtime.runs.save

    async def fail_update_save(state, version):
        if state.working_state is not None:
            raise RuntimeError("storage unavailable")
        await save(state, version)

    runtime.runs.save = fail_update_save
    action = PlannedAction(
        id=f"tool-{call.id}",
        type=ActionType.TOOL,
        description="Update progress",
        tool_name=WORKING_STATE_TOOL,
        arguments=call.arguments,
    )
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await runtime._execute_tool(current, agent(), action)
    checkpoint = await runtime.status(current.id, "tenant")
    assert checkpoint.working_state is None
    assert call.id not in checkpoint.completed_tool_call_ids
    assert not [
        e
        for e in await runtime.events.history(current.id)
        if e.type == "context.working_state.updated"
    ]
    runtime.runs.save = save
    checkpoint.status = RunStatus.PLANNING
    await runtime._execute_tool(checkpoint, agent(), action)
    restored = await runtime.status(current.id, "tenant")
    assert restored.working_state.revision == 1
    assert call.id in restored.completed_tool_call_ids

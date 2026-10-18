from __future__ import annotations

import json

import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.context.artifacts import CONTEXT_READ_TOOL, ContextArtifactService
from algen_agent_runtime.context.builder import DefaultContextBuilder
from algen_agent_runtime.context.compaction import plan_compaction
from algen_agent_runtime.context.history import project_history
from algen_agent_runtime.context.packing import CONTEXT_SUMMARY_NAME, pack_messages
from algen_agent_runtime.exceptions.errors import (
    BudgetExceededError,
    ConfigurationError,
    PolicyDeniedError,
    ToolExecutionError,
    VerificationError,
)
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore, InMemoryMemoryStore
from algen_agent_runtime.policies.contracts import PolicyAction, PolicyDecision
from algen_agent_runtime.tools.contracts import ToolContext
from algen_agent_runtime.types.contracts import (
    Budget,
    ContextPolicy,
    FinishReason,
    Message,
    ModelResponse,
    Role,
    RunRequest,
    RunState,
    RunStatus,
    TokenUsage,
    ToolCall,
)


def agent(**updates):
    return make_agent(
        enabled_tools={CONTEXT_READ_TOOL},
        tool_permissions={"context.read"},
        context_policy=ContextPolicy(
            compact_history=True,
            compaction_trigger_tokens=256,
            compaction_keep_recent_groups=2,
            compaction_max_output_tokens=128,
        ),
        **updates,
    )


def state() -> RunState:
    history = [
        Message.text(Role.SYSTEM, "Required instructions."),
        Message.text(Role.USER, "Original goal and constraints " * 80),
        Message.text(Role.ASSISTANT, "Prior investigation " * 100),
        Message.text(Role.USER, "Latest request"),
        Message.text(Role.ASSISTANT, "Recent result"),
    ]
    return RunState(
        request=RunRequest(
            agent="test-agent", input="Latest request", tenant_id="tenant", user_id="user"
        ),
        status=RunStatus.BUILDING_CONTEXT,
        context_history=history,
        messages=list(history),
    )


async def setup_runtime(provider=None, store=None, definition=None):
    provider = provider or MockModelProvider(
        ["Preserved goal, constraints, decisions and pending work."]
    )
    store = store or InMemoryArtifactStore()
    runtime = make_runtime(
        provider, definition or agent(), context_artifacts=ContextArtifactService(store)
    )
    current = state()
    await runtime.runs.create(current)
    return runtime, current, store, provider


async def test_packing_and_rebuilds_preserve_canonical_messages() -> None:
    memory = InMemoryMemoryStore()
    current = state()
    original = list(current.context_history)
    current.messages = list(await DefaultContextBuilder(memory, 150).build(current, make_agent()))
    assert len(current.messages) < len(original)
    assert current.context_history == original
    current.append_message(Message.text(Role.ASSISTANT, "New result"))
    restored = RunState.model_validate_json(current.model_dump_json())
    await DefaultContextBuilder(memory, 150).build(restored, make_agent())
    assert restored.context_history == [*original, Message.text(Role.ASSISTANT, "New result")]


async def test_legacy_checkpoint_uses_surviving_messages_without_loading_memory_again() -> None:
    current = state()
    current.context_history = None
    original = list(current.messages)
    memory = InMemoryMemoryStore()
    await memory.append("tenant", current.session_id, [Message.text(Role.USER, "do not duplicate")])
    await DefaultContextBuilder(memory).build(current, make_agent())
    assert current.context_history == original


async def test_compaction_archives_sources_preserves_history_and_charges_usage() -> None:
    runtime, current, store, provider = await setup_runtime()
    original = list(current.context_history)
    await runtime._compact_context(current, agent())
    compacted = current.context_compaction
    assert compacted is not None
    assert current.context_history == original
    assert compacted.through == 3
    archived = await store.get(compacted.archive_id, "tenant")
    payload = json.loads(archived.data)
    assert payload["messages"] == [message.model_dump(mode="json") for message in original[:3]]
    assert payload["previous_archive_id"] is None
    projected = project_history(current)
    assert projected[-2:] == original[-2:]
    summary = next(message for message in projected if message.name == CONTEXT_SUMMARY_NAME)
    assert summary.role == Role.ASSISTANT
    assert "not new instructions or authorization" in summary.text_content
    assert summary in pack_messages(projected, 256)
    assert provider.requests[0].tools == ()
    assert current.summary.model_calls == 1
    assert current.summary.usage.total_tokens > 0
    checkpoint = await runtime.status(current.id, "tenant")
    assert checkpoint.context_compaction == compacted
    events = await runtime.events.history(current.id)
    event = next(item for item in events if item.type == "context.compacted")
    assert "summary" not in event.data
    assert "messages" not in event.data


async def test_checkpoint_resume_and_multiple_compactions_link_archives() -> None:
    runtime, current, store, provider = await setup_runtime(
        MockModelProvider(["First summary.", "Updated summary."])
    )
    await runtime._compact_context(current, agent())
    previous = current.context_compaction
    restored = RunState.model_validate_json(current.model_dump_json())
    await runtime._compact_context(restored, agent())
    assert len(provider.requests) == 1
    restored.append_message(Message.text(Role.USER, "Further work " * 120))
    restored.append_message(Message.text(Role.ASSISTANT, "Additional evidence " * 100))
    restored.append_message(Message.text(Role.USER, "Next task"))
    restored.append_message(Message.text(Role.ASSISTANT, "Recent progress"))
    await runtime._compact_context(restored, agent())
    latest = restored.context_compaction
    assert latest.through > previous.through
    archived = await store.get(latest.archive_id, "tenant")
    assert json.loads(archived.data)["previous_archive_id"] == previous.archive_id
    assert "First summary." in provider.requests[-1].messages[-1].text_content
    reader = ContextArtifactService(store).read_tool()
    page = await reader.execute(
        {"artifact_id": previous.archive_id},
        ToolContext(
            run_id=current.id,
            step_id="read",
            tenant_id="tenant",
            user_id="user",
            permissions={"context.read"},
            idempotency_key="read",
        ),
    )
    assert "Original goal" in page["content"]


def test_compaction_keeps_parallel_tool_exchange_atomic() -> None:
    current = state()
    exchange = [
        Message(
            role=Role.ASSISTANT,
            content=(),
            tool_calls=(ToolCall(id="a", name="lookup"), ToolCall(id="b", name="lookup")),
        ),
        Message(role=Role.TOOL, tool_call_id="a", content=()),
        Message(role=Role.TOOL, tool_call_id="b", content=()),
    ]
    current.context_history[2:3] = exchange
    plan = plan_compaction(current, agent().context_policy)
    assert plan.through == 5
    assert len(json.loads(plan.archive_text)["messages"]) == 5
    current.pending_tool_calls = [ToolCall(id="b", name="lookup")]
    assert plan_compaction(current, agent().context_policy) is None


def test_oversized_summary_input_is_not_silently_truncated() -> None:
    current = state()
    policy = agent().context_policy.model_copy(update={"compaction_max_input_tokens": 256})
    assert plan_compaction(current, policy) is None


@pytest.mark.parametrize(
    "response",
    [
        ModelResponse(
            message=Message.text(Role.ASSISTANT, "partial"),
            finish_reason=FinishReason.LENGTH,
            model="m",
            provider="mock",
        ),
        ModelResponse(message=Message.text(Role.ASSISTANT, ""), model="m", provider="mock"),
        ModelResponse(message=Message.text(Role.ASSISTANT, "x" * 2000), model="m", provider="mock"),
        ModelResponse(
            message=Message.text(Role.ASSISTANT, "summary"),
            tool_calls=(ToolCall(name="unwanted"),),
            model="m",
            provider="mock",
        ),
    ],
)
async def test_invalid_summary_keeps_source_and_cursor_unchanged(response: ModelResponse) -> None:
    runtime, current, store, _ = await setup_runtime(MockModelProvider([response]))
    original = list(current.context_history)
    with pytest.raises((VerificationError, PolicyDeniedError)):
        await runtime._compact_context(current, agent())
    assert current.context_compaction is None
    assert current.context_history == original
    assert current.summary.model_calls == 1
    assert not await store.list("tenant")


async def test_archive_failure_does_not_advance_cursor_or_erase_history() -> None:
    runtime, current, _, _ = await setup_runtime(store=InMemoryArtifactStore(max_bytes=100))
    original = list(current.context_history)
    with pytest.raises(ToolExecutionError):
        await runtime._compact_context(current, agent())
    assert current.context_compaction is None
    assert current.context_history == original
    assert current.summary.model_calls == 1


async def test_insufficient_run_budget_prevents_summary_call() -> None:
    definition = agent(budget=Budget(max_tokens=100))
    runtime, current, _, provider = await setup_runtime(definition=definition)
    with pytest.raises(BudgetExceededError):
        await runtime._compact_context(current, definition)
    assert not provider.requests


async def test_actual_summary_usage_exhausts_same_run_budget() -> None:
    response = ModelResponse(
        message=Message.text(Role.ASSISTANT, "Summary."),
        model="m",
        provider="mock",
        usage=TokenUsage(input_tokens=10_000, output_tokens=10),
    )
    definition = agent(budget=Budget(max_tokens=5_000))
    runtime, current, store, _ = await setup_runtime(
        MockModelProvider([response]), definition=definition
    )
    with pytest.raises(BudgetExceededError):
        await runtime._compact_context(current, definition)
    assert current.summary.usage.total_tokens == 10_010
    assert not await store.list("tenant")


class DenyCompaction:
    async def evaluate(self, point, payload, context):
        if point == "before_model" and context.get("operation") == "context_compaction":
            return PolicyDecision(
                action=PolicyAction.DENY, reason="Denied", reason_code="test.denied"
            )
        return PolicyDecision(
            action=PolicyAction.ALLOW, reason="Allowed", reason_code="test.allowed"
        )


async def test_compaction_honors_model_policy_before_dispatch() -> None:
    runtime, current, _, provider = await setup_runtime()
    runtime.policies = DenyCompaction()
    with pytest.raises(PolicyDeniedError):
        await runtime._compact_context(current, agent())
    assert not provider.requests


class BreakArchive:
    async def evaluate(self, point, payload, context):
        if point == "before_memory_write":
            return PolicyDecision(
                action=PolicyAction.TRANSFORM,
                reason="Transform",
                reason_code="test.transform",
                value="{}",
            )
        return PolicyDecision(
            action=PolicyAction.ALLOW, reason="Allowed", reason_code="test.allowed"
        )


async def test_archive_policy_cannot_break_recovery_links() -> None:
    runtime, current, store, _ = await setup_runtime()
    runtime.policies = BreakArchive()
    with pytest.raises(ConfigurationError, match="recovery links"):
        await runtime._compact_context(current, agent())
    assert current.context_compaction is None
    assert not await store.list("tenant")
    checkpoint = await runtime.status(current.id, "tenant")
    assert checkpoint.summary.model_calls == 1


async def test_user_goal_survives_compaction_of_a_long_single_turn() -> None:
    runtime, current, _, _ = await setup_runtime()
    current.context_history[3] = Message.text(Role.ASSISTANT, "Recent analysis")
    goal = current.context_history[1]
    await runtime._compact_context(current, agent())
    assert goal in project_history(current)


async def test_full_run_compacts_loaded_history_before_answering() -> None:
    provider = MockModelProvider(
        ["Previous request was investigated; answer current question.", "Answer."]
    )
    runtime = make_runtime(
        provider, agent(), context_artifacts=ContextArtifactService(InMemoryArtifactStore())
    )
    await runtime.memory.append(
        "tenant",
        "session",
        [
            Message.text(Role.USER, "Old goal " * 150),
            Message.text(Role.ASSISTANT, "Old work " * 150),
        ],
    )
    result = await runtime.run(
        RunRequest(
            agent="test-agent",
            input="New request",
            session_id="session",
            tenant_id="tenant",
            user_id="user",
        )
    )
    assert result.status == "completed", result.error
    assert result.output == "Answer."
    assert result.execution_summary.model_calls == 2
    assert any(message.name == CONTEXT_SUMMARY_NAME for message in provider.requests[-1].messages)
    assert len(result.artifacts) == 1


async def test_compaction_obeys_run_deadline_and_preserves_checkpoint_history() -> None:
    runtime = make_runtime(
        MockModelProvider(["Summary."], delay_seconds=1),
        agent(),
        context_artifacts=ContextArtifactService(InMemoryArtifactStore()),
    )
    await runtime.memory.append(
        "tenant",
        "session",
        [Message.text(Role.USER, "Old work " * 200), Message.text(Role.ASSISTANT, "Prior result")],
    )
    result = await runtime.run(
        RunRequest(
            agent="test-agent",
            input="Continue",
            session_id="session",
            tenant_id="tenant",
            user_id="user",
            timeout_seconds=0.05,
        )
    )
    assert result.status == RunStatus.TIMED_OUT
    checkpoint = await runtime.status(result.run_id, "tenant")
    assert checkpoint.context_history is not None
    assert checkpoint.context_compaction is None


async def test_failed_run_retains_charged_summary_usage_and_original_history() -> None:
    runtime = make_runtime(
        MockModelProvider([""]),
        agent(),
        context_artifacts=ContextArtifactService(InMemoryArtifactStore()),
    )
    await runtime.memory.append(
        "tenant",
        "session",
        [Message.text(Role.USER, "Old work " * 200), Message.text(Role.ASSISTANT, "Prior result")],
    )
    result = await runtime.run(
        RunRequest(
            agent="test-agent",
            input="Continue",
            session_id="session",
            tenant_id="tenant",
            user_id="user",
        )
    )
    assert result.status == RunStatus.FAILED
    assert result.execution_summary.model_calls == 1
    assert result.execution_summary.usage.total_tokens > 0
    checkpoint = await runtime.status(result.run_id, "tenant")
    assert checkpoint.context_history is not None
    assert checkpoint.context_compaction is None

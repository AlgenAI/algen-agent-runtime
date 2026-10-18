from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from test_context_compaction import agent, setup_runtime, state

from algen_agent_runtime.context.budget import estimate_request_tokens, message_token_budget
from algen_agent_runtime.context.builder import DefaultContextBuilder, RetrievalContextBuilder
from algen_agent_runtime.context.history import project_history
from algen_agent_runtime.exceptions.errors import ConflictError, ToolExecutionError
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.types.contracts import (
    Budget,
    ContextPolicy,
    Message,
    ModelProfile,
    Role,
    RunState,
    WorkingState,
)


def configured(**updates):
    definition = agent()
    return definition.model_copy(
        update={"context_policy": definition.context_policy.model_copy(update=updates)}
    )


async def test_archived_checkpoints_shrink_and_recover_the_exact_source_chain() -> None:
    definition = configured(archive_compacted_history=True)
    runtime, current, store, provider = await setup_runtime(
        MockModelProvider(["Summary."] * 5), definition=definition
    )
    canonical = list(current.context_history)
    initial_size = len(current.model_dump_json())
    for index in range(5):
        await runtime._compact_context(current, definition)
        checkpoint = await runtime.status(current.id, "tenant")
        current = RunState.model_validate_json(checkpoint.model_dump_json())
        assert current.context_history_offset == len(canonical) - 2
        assert current.context_history == canonical[-2:]
        assert current.context_history_pinned == canonical[:1]
        assert len(current.model_dump_json()) < initial_size // 2
        assert len(provider.requests) == index + 1
        if index < 4:
            messages = [
                Message.text(Role.USER, "Investigation " * 160),
                Message.text(Role.ASSISTANT, "Evidence " * 160),
                Message.text(Role.USER, f"Next request {index}"),
                Message.text(Role.ASSISTANT, "Latest result"),
            ]
            canonical.extend(messages)
            for message in messages:
                current.append_message(message)

    # Rebuilding a recovered checkpoint must not resummarize already archived messages.
    await runtime._compact_context(current, definition)
    assert len(provider.requests) == 5
    chunks = []
    archive_id = current.context_compaction.archive_id
    through = current.context_history_offset
    while archive_id is not None:
        payload = json.loads((await store.get(archive_id, "tenant")).data)
        assert payload["through"] == through
        through = payload["start"]
        chunks.insert(0, [Message.model_validate(message) for message in payload["messages"]])
        archive_id = payload["previous_archive_id"]
    assert through == 0
    assert [message for chunk in chunks for message in chunk] + current.context_history == canonical


async def test_archived_goal_stays_verbatim_until_a_new_user_message() -> None:
    definition = configured(archive_compacted_history=True)
    runtime, current, _, _ = await setup_runtime(definition=definition)
    current.context_history[3] = Message.text(Role.ASSISTANT, "Recent analysis")
    goal = current.context_history[1]
    await runtime._compact_context(current, definition)
    assert goal in current.context_history_pinned
    restored = RunState.model_validate_json(current.model_dump_json())
    assert project_history(restored) == project_history(current)
    assert goal in project_history(restored)
    restored.append_message(Message.text(Role.USER, "Revised goal"))
    assert goal not in project_history(restored)
    assert project_history(restored)[-1].text_content == "Revised goal"


async def test_archive_failure_keeps_inline_source_in_durable_checkpoint() -> None:
    definition = configured(archive_compacted_history=True)
    runtime, current, _, _ = await setup_runtime(
        store=InMemoryArtifactStore(max_bytes=100), definition=definition
    )
    original = list(current.context_history)
    with pytest.raises(ToolExecutionError):
        await runtime._compact_context(current, definition)
    checkpoint = await runtime.status(current.id, "tenant")
    assert checkpoint.context_history == original
    assert checkpoint.context_history_offset == 0
    assert checkpoint.context_compaction is None
    assert checkpoint.summary.model_calls == 1


async def test_checkpoint_failure_after_archive_write_can_recover_without_losing_sources(
    monkeypatch,
) -> None:
    definition = configured(archive_compacted_history=True)
    runtime, current, store, provider = await setup_runtime(
        MockModelProvider(["Summary."] * 2), definition=definition
    )
    original = list(current.context_history)
    save = runtime.runs.save
    calls = 0

    async def fail_after_usage(state, version):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ConflictError("checkpoint interrupted")
        await save(state, version)

    monkeypatch.setattr(runtime.runs, "save", fail_after_usage)
    with pytest.raises(ConflictError, match="interrupted"):
        await runtime._compact_context(current, definition)
    restored = await runtime.status(current.id, "tenant")
    assert restored.context_history == original
    assert restored.context_history_offset == 0
    assert restored.summary.model_calls == 1
    assert not any(
        event.type == "context.compacted" for event in await runtime.events.history(current.id)
    )
    await runtime._compact_context(restored, definition)
    assert restored.context_history == original[-2:]
    assert restored.context_history_offset == 3
    assert len(await store.list("tenant")) == 1
    assert len(provider.requests) == 2


async def test_archived_history_supports_disabling_pruning_on_resume() -> None:
    runtime, current, _, _ = await setup_runtime(MockModelProvider(["Summary."] * 2))
    await runtime._compact_context(current, configured(archive_compacted_history=True))
    offset = current.context_history_offset
    for message in state().context_history[1:]:
        current.append_message(message)
    await runtime._compact_context(current, agent())
    assert current.context_history_offset == offset
    assert current.context_compaction.through > offset
    restored = RunState.model_validate_json(current.model_dump_json())
    assert project_history(restored) == project_history(current)
    assert project_history(restored)[-2:] == state().context_history[-2:]


def test_legacy_checkpoint_defaults_and_invalid_archived_state() -> None:
    payload = state().model_dump(mode="json")
    del payload["context_history_offset"]
    del payload["context_history_pinned"]
    restored = RunState.model_validate(payload)
    assert restored.context_history_offset == 0
    assert restored.context_history_pinned == []
    payload["context_history_offset"] = 3
    with pytest.raises(ValidationError, match="compaction cursor"):
        RunState.model_validate(payload)


@pytest.mark.parametrize("ratio", [0, -1, 1.01])
def test_invalid_tuning_ratio_is_rejected(ratio) -> None:
    with pytest.raises(ValidationError):
        ContextPolicy(compaction_trigger_ratio=ratio)


def test_archive_mode_requires_compaction() -> None:
    with pytest.raises(ValidationError, match="requires compact_history"):
        ContextPolicy(archive_compacted_history=True)


async def test_builder_limit_triggers_compaction_below_manual_threshold() -> None:
    definition = configured(compaction_trigger_tokens=12000)
    runtime, current, _, provider = await setup_runtime(definition=definition)
    runtime.contexts.register(DefaultContextBuilder(runtime.memory, 1000))
    await runtime._compact_context(current, definition)
    assert len(provider.requests) == 1
    event = next(
        event
        for event in await runtime.events.history(current.id)
        if event.type == "context.compacted"
    )
    assert event.data["trigger_tokens"] == 800


async def test_fixed_threshold_opt_out_and_large_budget_do_not_compact() -> None:
    definition = configured(compaction_trigger_tokens=12000, compaction_trigger_ratio=None)
    runtime, current, _, provider = await setup_runtime(definition=definition)
    runtime.contexts.register(DefaultContextBuilder(runtime.memory, 1000))
    await runtime._compact_context(current, definition)
    assert not provider.requests
    runtime.contexts.register(DefaultContextBuilder(runtime.memory, 16000))
    await runtime._compact_context(current, configured(compaction_trigger_tokens=12000))
    assert not provider.requests


async def test_model_allowance_includes_tools_schema_output_and_extensions() -> None:
    profile = ModelProfile(
        name="small",
        provider="mock",
        model="deterministic",
        context_window_tokens=2200,
        context_safety_margin_tokens=100,
        extensions={"format_hint": "text " * 30},
    )
    definition = configured(compaction_trigger_tokens=12000).model_copy(
        update={"default_model": profile, "budget": Budget(max_output_tokens=400)}
    )
    runtime, current, _, provider = await setup_runtime(definition=definition)
    current.request = current.request.model_copy(
        update={
            "overrides": current.request.overrides.model_copy(
                update={
                    "response_schema": {"type": "object", "description": "Schema " * 100},
                    "max_output_tokens": 500,
                }
            )
        }
    )
    assembled = await runtime.contexts.get("default").assemble(current, definition)
    request = runtime._model_request(current, definition, assembled)
    expected = int(message_token_budget(request, profile) * 0.8)
    await runtime._compact_context(current, definition)
    assert len(provider.requests) == 1
    event = next(
        event
        for event in await runtime.events.history(current.id)
        if event.type == "context.compacted"
    )
    assert event.data["trigger_tokens"] == expected
    assert expected < int((2200 - 500 - 100) * 0.8)


@pytest.mark.parametrize("allowed", [True, False])
async def test_only_allowed_fallbacks_influence_trigger(allowed) -> None:
    definition = configured(compaction_trigger_tokens=12000).model_copy(
        update={
            "budget": Budget(max_output_tokens=128),
            "fallback_models": (
                ModelProfile(
                    name="small", provider="mock", model="fallback", context_window_tokens=1800
                ),
            ),
            "model_allowlist": frozenset() if allowed else frozenset({"mock/deterministic"}),
        }
    )
    runtime, current, _, provider = await setup_runtime(definition=definition)
    await runtime._compact_context(current, definition)
    assert bool(provider.requests) == allowed


async def test_working_state_counts_towards_adaptive_trigger() -> None:
    definition = configured(compaction_trigger_tokens=12000, working_state_enabled=True)
    runtime, current, _, provider = await setup_runtime(definition=definition)
    runtime.contexts.register(DefaultContextBuilder(runtime.memory, 2000))
    await runtime._compact_context(current, definition)
    assert not provider.requests
    current.working_state = WorkingState.model_validate(
        {
            "state": {
                "goal": "Active goal " * 120,
                "constraints": [],
                "decisions": [],
                "completed_work": [],
                "open_tasks": [],
            }
        }
    )
    await runtime._compact_context(current, definition)
    assert len(provider.requests) == 1


async def test_retrieval_reserves_budget_without_retrieving_twice() -> None:
    class Retriever:
        calls = 0

        async def retrieve(self, query):
            self.calls += 1
            return ()

    retriever = Retriever()
    definition = configured(compaction_trigger_tokens=12000).model_copy(
        update={"context_builder": "retrieval"}
    )
    runtime, current, _, provider = await setup_runtime(definition=definition)
    builder = RetrievalContextBuilder(
        "retrieval", runtime.memory, retriever, max_context_tokens=3000, max_retrieval_tokens=2000
    )
    runtime.contexts.register(builder)
    await runtime._compact_context(current, definition)
    assert len(provider.requests) == 1
    assert retriever.calls == 0
    await builder.build(current, definition)
    assert retriever.calls == 1


async def test_small_model_summarizes_a_bounded_prefix_instead_of_overflowing() -> None:
    definition = configured(archive_compacted_history=True).model_copy(
        update={
            "default_model": ModelProfile(
                name="small", provider="mock", model="small", context_window_tokens=2400
            ),
            "budget": Budget(max_output_tokens=128),
        }
    )
    runtime, current, _, provider = await setup_runtime(definition=definition)
    current.context_history = [
        Message.text(Role.SYSTEM, "Required instructions"),
        *[Message.text(Role.ASSISTANT, "Evidence " * 100) for _ in range(20)],
        Message.text(Role.USER, "Current request"),
        Message.text(Role.ASSISTANT, "Recent result"),
    ]
    original_count = len(current.context_history)
    await runtime._compact_context(current, definition)
    assert len(provider.requests) == 1
    assert estimate_request_tokens(provider.requests[0]) + 128 + 256 <= 2400
    assert 1 < current.context_history_offset < original_count - 2
    assert current.context_history_offset + len(current.context_history) == original_count

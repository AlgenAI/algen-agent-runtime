from __future__ import annotations

import json

import pytest
from conftest import make_agent, make_runtime
from pydantic import ValidationError

from algen_agent_runtime.config.registry import InMemoryAgentRegistry
from algen_agent_runtime.context.artifacts import ContextArtifactService
from algen_agent_runtime.context.builder import DefaultContextBuilder, RetrievalContextBuilder
from algen_agent_runtime.context.packing import (
    LOADED_SKILLS_NAME,
    SKILL_CATALOG_NAME,
    pack_messages,
)
from algen_agent_runtime.context.skills import SKILL_LOAD_TOOL, skill_context_messages
from algen_agent_runtime.exceptions.errors import (
    BudgetExceededError,
    ConfigurationError,
    PolicyDeniedError,
)
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore, InMemoryMemoryStore
from algen_agent_runtime.planning.contracts import ActionType, PlannedAction
from algen_agent_runtime.policies.contracts import PolicyAction, PolicyDecision
from algen_agent_runtime.tools.contracts import ToolContext
from algen_agent_runtime.types.contracts import (
    ContextPolicy,
    LoadedSkill,
    LoadedSkills,
    Message,
    Role,
    RunRequest,
    RunState,
    RunStatus,
    SkillDefinition,
    SkillSelection,
)


def skill(name="report", **updates):
    return SkillDefinition(
        **{
            "name": name,
            "version": "1.0.0",
            "description": f"Prepare a {name}.",
            "instructions": f"Detailed {name} procedure: verify sources, then compare totals.",
            **updates,
        }
    )


def agent(**updates):
    return make_agent(
        **{
            "skills": (skill(), skill("review")),
            "enabled_tools": {SKILL_LOAD_TOOL},
            "tool_permissions": {"skills.read"},
            "context_policy": ContextPolicy(skills_enabled=True),
            **updates,
        }
    )


def request(**updates):
    return RunRequest(
        **{
            "agent": "test-agent",
            "tenant_id": "tenant",
            "user_id": "user",
            "input": "Prepare the report",
            **updates,
        }
    )


def loaded(source=None):
    source = source or skill()
    return LoadedSkills(
        skills=(
            LoadedSkill(
                name=source.name,
                version=source.version,
                source_sha256=source.sha256,
                instructions=source.instructions,
            ),
        )
    )


def messages(request, name):
    return [m for m in request.messages if m.name == name]


async def test_progressive_loading_persists_only_selected_details_without_extra_model_call():
    provider = MockModelProvider(
        [tool_call_response(SKILL_LOAD_TOOL, {"names": ["report"]}), "Done"]
    )
    runtime = make_runtime(provider, agent())
    result = await runtime.run(request())
    assert result.status == RunStatus.COMPLETED, result.error
    initial = provider.requests[0]
    assert len(messages(initial, SKILL_CATALOG_NAME)) == 1
    assert not messages(initial, LOADED_SKILLS_NAME)
    assert "Prepare a report" in messages(initial, SKILL_CATALOG_NAME)[0].text_content
    assert "Detailed report procedure" not in str(initial)
    assert (
        "Detailed report procedure"
        in messages(provider.requests[1], LOADED_SKILLS_NAME)[0].text_content
    )
    assert "Detailed review procedure" not in str(provider.requests[1])
    checkpoint = await runtime.status(result.run_id, "tenant")
    assert checkpoint.loaded_skills == loaded()
    assert not any(
        m.name in {SKILL_CATALOG_NAME, LOADED_SKILLS_NAME} for m in checkpoint.context_history
    )
    assert "Detailed report procedure" not in str(checkpoint.context_history)
    receipt = next(m for m in checkpoint.context_history if m.role == Role.TOOL)
    assert json.loads(receipt.text_content) == {"loaded": [{"name": "report", "version": "1.0.0"}]}
    assert result.execution_summary.model_calls == 2
    events = await runtime.events.history(result.run_id)
    assert next(e for e in events if e.type == "context.skills.updated").data == {"count": 1}
    assert RunState.model_validate_json(checkpoint.model_dump_json()).loaded_skills == loaded()


async def test_selection_replaces_previous_skills_and_empty_list_unloads():
    provider = MockModelProvider(
        [
            tool_call_response(SKILL_LOAD_TOOL, {"names": ["report", "review"]}),
            tool_call_response(SKILL_LOAD_TOOL, {"names": ["review"]}),
            tool_call_response(SKILL_LOAD_TOOL, {"names": []}),
            "Done",
        ]
    )
    runtime = make_runtime(provider, agent(max_steps=12))
    result = await runtime.run(request())
    assert result.status == RunStatus.COMPLETED, result.error
    assert "Detailed report procedure" in str(provider.requests[1])
    assert "Detailed report procedure" not in str(provider.requests[2])
    assert "Detailed review procedure" in str(provider.requests[2])
    assert not messages(provider.requests[3], LOADED_SKILLS_NAME)
    assert not (await runtime.status(result.run_id, "tenant")).loaded_skills.skills


async def test_default_agents_do_not_advertise_skills():
    provider = MockModelProvider(["Done"])
    runtime = make_runtime(provider)
    result = await runtime.run(request())
    assert result.status == RunStatus.COMPLETED
    assert not messages(provider.requests[0], SKILL_CATALOG_NAME)
    assert not any(t.name == SKILL_LOAD_TOOL for t in provider.requests[0].tools)


@pytest.mark.parametrize(
    "updates",
    [
        {"enabled_tools": set()},
        {"tool_permissions": set()},
        {"context_policy": ContextPolicy()},
    ],
)
async def test_feature_requires_explicit_enablement_and_permission(updates):
    runtime = make_runtime(agent=agent(**updates))
    with pytest.raises(ConfigurationError, match="skills"):
        await runtime.start(request())


async def test_catalog_hides_inaccessible_skills_and_loading_them_is_denied():
    hidden = skill("restricted", required_permissions={"private.reports"})
    definition = agent(skills=(skill(), hidden))
    current = RunState(request=request())
    assert "restricted" not in skill_context_messages(current, definition)[0].text_content
    runtime = make_runtime(
        MockModelProvider([tool_call_response(SKILL_LOAD_TOOL, {"names": ["restricted"]})]),
        definition,
    )
    result = await runtime.run(request())
    assert result.status == RunStatus.FAILED
    assert not (await runtime.status(result.run_id, "tenant")).loaded_skills.skills


@pytest.mark.parametrize(
    "names", [["missing"], ["report", "report"], ["report"] * 5, ["../SKILL.md"]]
)
async def test_invalid_selection_never_updates_run(names):
    runtime = make_runtime(
        MockModelProvider([tool_call_response(SKILL_LOAD_TOOL, {"names": names})]), agent()
    )
    result = await runtime.run(request())
    assert result.status == RunStatus.FAILED
    assert not (await runtime.status(result.run_id, "tenant")).loaded_skills.skills


async def create_tool_run(runtime, **updates):
    state = RunState(request=request(**updates))
    await runtime.runs.create(state)
    context = ToolContext(
        run_id=state.id,
        step_id="load",
        tenant_id=state.request.tenant_id,
        user_id=state.request.user_id,
        permissions={"skills.read"},
        idempotency_key=f"{state.id}:load",
    )
    return state, context


@pytest.mark.parametrize(
    "change", [{"tenant_id": "other"}, {"user_id": "other"}, {"run_id": "missing"}]
)
async def test_direct_loader_and_replay_enforce_run_tenant_user(change):
    runtime = make_runtime(agent=agent())
    _, context = await create_tool_run(runtime)
    tool = runtime.tools.get(SKILL_LOAD_TOOL)
    result = await runtime.tool_executor.execute(SKILL_LOAD_TOOL, {"names": ["report"]}, context)
    bad = context.model_copy(update=change)
    with pytest.raises(PolicyDeniedError):
        await tool.execute({"names": ["report"]}, bad)
    with pytest.raises(PolicyDeniedError):
        await tool.validate_replay(result, bad)


async def test_skill_allowlist_is_specific_to_the_run_agent():
    runtime = make_runtime(agent=agent())
    runtime.agents.register(agent(name="other-agent", skills=(skill("other"),)))
    _, context = await create_tool_run(runtime, agent="other-agent")
    with pytest.raises(PolicyDeniedError):
        await runtime.tools.get(SKILL_LOAD_TOOL).execute({"names": ["report"]}, context)


@pytest.mark.parametrize(
    "replacement",
    [
        agent(skills=()),
        agent(skills=(skill(instructions="Changed without a version bump"),)),
        agent(skills=(skill(required_permissions={"revoked"}),)),
    ],
)
async def test_replay_and_recovery_reject_removed_changed_or_revoked_skills(replacement):
    runtime = make_runtime(agent=agent())
    current, context = await create_tool_run(runtime)
    await runtime.tool_executor.execute(SKILL_LOAD_TOOL, {"names": ["report"]}, context)
    runtime.agents = InMemoryAgentRegistry()
    runtime.agents.register(replacement)
    with pytest.raises(PolicyDeniedError):
        await runtime.tool_executor.execute(SKILL_LOAD_TOOL, {"names": ["report"]}, context)
    current.loaded_skills = loaded()
    current.status = RunStatus.PLANNING
    with pytest.raises(PolicyDeniedError):
        await runtime._execute(current, replacement)


async def test_catalog_and_loaded_instruction_budgets_are_independent():
    current = RunState(request=request())
    definition = agent(
        skills=(skill(description="x" * 512), skill("review", description="x" * 512)),
        context_policy=ContextPolicy(skills_enabled=True, skill_catalog_max_tokens=128),
    )
    with pytest.raises(BudgetExceededError, match="skill_catalog_max_tokens"):
        skill_context_messages(current, definition)
    long_skill = skill(instructions="x" * 6000)
    provider = MockModelProvider([tool_call_response(SKILL_LOAD_TOOL, {"names": ["report"]})])
    runtime = make_runtime(
        provider,
        agent(
            skills=(long_skill,),
            context_policy=ContextPolicy(skills_enabled=True, loaded_skills_max_tokens=256),
        ),
    )
    result = await runtime.run(request())
    assert result.status == RunStatus.FAILED
    assert not (await runtime.status(result.run_id, "tenant")).loaded_skills.skills
    assert "xxxx" not in str(provider.requests[0])


async def test_loaded_context_survives_packing_rebuilds_and_retrieval():
    class EmptyRetriever:
        async def retrieve(self, query):
            return ()

    current = RunState(request=request(), loaded_skills=loaded())
    memory = InMemoryMemoryStore()
    for builder in [
        DefaultContextBuilder(memory),
        RetrievalContextBuilder("retrieval", memory, EmptyRetriever()),
    ]:
        for _ in range(2):
            built = await builder.build(current, agent())
            assert len([m for m in built if m.name == LOADED_SKILLS_NAME]) == 1
            assert all(
                m.role == Role.ASSISTANT
                for m in built
                if m.name in {LOADED_SKILLS_NAME, SKILL_CATALOG_NAME}
            )
            packed = pack_messages(
                [*built[:-1], Message.text(Role.USER, "Old " * 1000), built[-1]], 600
            )
            assert any(m.name == LOADED_SKILLS_NAME for m in packed)
            assert not any("Old " in m.text_content for m in packed)
    assert not any(m.name == LOADED_SKILLS_NAME for m in current.context_history)
    with pytest.raises(BudgetExceededError):
        pack_messages(built, 50)


class SkillPolicy:
    def __init__(self, mode, point="before_memory_write"):
        self.mode, self.point = mode, point

    async def evaluate(self, point, payload, context):
        if point == self.point and (
            point != "before_memory_write" or context.get("operation") == "skills_load"
        ):
            if self.mode == "deny":
                return PolicyDecision(
                    action=PolicyAction.DENY, reason="Denied", reason_code="test.denied"
                )
            value = json.loads(json.dumps(payload))
            if self.mode == "redact":
                value["skills"][0]["instructions"] = "Redacted guidance"
            elif self.mode == "identity":
                value["skills"][0]["source_sha256"] = "0" * 64
            elif self.mode == "selection":
                value["skills"] = []
            else:
                value["skills"][0]["instructions"] = "x" * 16000
            return PolicyDecision(
                action=PolicyAction.TRANSFORM,
                reason="Changed",
                reason_code="test.changed",
                value=value,
            )
        return PolicyDecision(
            action=PolicyAction.ALLOW, reason="Allowed", reason_code="test.allowed"
        )


@pytest.mark.parametrize("point", ["after_tool", "before_memory_write"])
async def test_governance_redacts_loaded_instructions(point):
    provider = MockModelProvider(
        [tool_call_response(SKILL_LOAD_TOOL, {"names": ["report"]}), "Done"]
    )
    runtime = make_runtime(provider, agent())
    policy = SkillPolicy("redact", point)
    runtime.policies = policy
    runtime.tool_executor._policy_engine = policy
    result = await runtime.run(request())
    assert result.status == RunStatus.COMPLETED, result.error
    assert (await runtime.status(result.run_id, "tenant")).loaded_skills.skills[
        0
    ].instructions == "Redacted guidance"
    assert "Redacted guidance" in str(provider.requests[-1])
    assert "Detailed report procedure" not in str(provider.requests[-1])


@pytest.mark.parametrize("mode", ["deny", "identity", "selection", "oversize"])
async def test_invalid_policy_result_preserves_checkpoint(mode):
    runtime = make_runtime(
        MockModelProvider([tool_call_response(SKILL_LOAD_TOOL, {"names": ["report"]})]), agent()
    )
    runtime.policies = SkillPolicy(mode)
    result = await runtime.run(request())
    assert result.status == RunStatus.FAILED
    assert not (await runtime.status(result.run_id, "tenant")).loaded_skills.skills


async def test_compaction_preserves_loaded_skills_without_archiving_their_projection():
    definition = agent(
        enabled_tools={SKILL_LOAD_TOOL, "core.context_read"},
        tool_permissions={"skills.read", "context.read"},
        context_policy=ContextPolicy(
            skills_enabled=True,
            compact_history=True,
            compaction_trigger_tokens=256,
            compaction_keep_recent_groups=2,
            compaction_max_output_tokens=128,
        ),
    )
    store = InMemoryArtifactStore()
    runtime = make_runtime(
        MockModelProvider(["Previous work summarized."]),
        definition,
        context_artifacts=ContextArtifactService(store),
    )
    history = [
        Message.text(Role.USER, "Old work " * 400),
        Message.text(Role.ASSISTANT, "Older result"),
        Message.text(Role.USER, "Latest request"),
        Message.text(Role.ASSISTANT, "Recent result"),
    ]
    current = RunState(
        request=request(),
        status=RunStatus.BUILDING_CONTEXT,
        context_history=history,
        loaded_skills=loaded(),
    )
    await runtime.runs.create(current)
    await runtime._compact_context(current, definition)
    assert current.context_compaction is not None
    assert current.loaded_skills == loaded()
    archive = await store.get(current.context_compaction.archive_id, "tenant")
    assert "Detailed report procedure" not in archive.data.decode()
    assert any(
        m.name == LOADED_SKILLS_NAME
        for m in await runtime.contexts.get("default").build(current, definition)
    )


async def test_recovery_of_recorded_load_checkpoints_selection_before_event():
    response = tool_call_response(SKILL_LOAD_TOOL, {"names": ["report"]})
    call = response.tool_calls[0]
    runtime = make_runtime(MockModelProvider(["Done"]), agent())
    current = RunState(request=request(), status=RunStatus.PLANNING, pending_tool_calls=[call])
    current.append_message(response.message.model_copy(update={"tool_calls": (call,)}))
    await runtime.runs.create(current)
    await runtime.tool_executor.execute(
        SKILL_LOAD_TOOL,
        call.arguments,
        ToolContext(
            run_id=current.id,
            step_id=f"tool-{call.id}",
            tenant_id="tenant",
            user_id="user",
            permissions={"skills.read"},
            idempotency_key=f"{current.id}:{call.id}",
        ),
    )
    original_emit = runtime._emit

    async def checked_emit(state, event_type, data, step_id=None):
        if event_type == "context.skills.updated":
            checkpoint = await runtime.status(state.id, "tenant")
            assert checkpoint.loaded_skills == loaded()
            assert call.id in checkpoint.completed_tool_call_ids
        return await original_emit(state, event_type, data, step_id)

    runtime._emit = checked_emit
    restored = RunState.model_validate_json(current.model_dump_json())
    await runtime._execute_tool(
        restored,
        agent(),
        PlannedAction(
            id=f"tool-{call.id}",
            type=ActionType.TOOL,
            description="Load",
            tool_name=SKILL_LOAD_TOOL,
            arguments=call.arguments,
        ),
    )
    resumed = make_runtime(MockModelProvider(["Done"]), agent())
    resumed.runs = runtime.runs
    await resumed._execute(await runtime.status(current.id, "tenant"), agent())
    assert (await runtime.status(current.id, "tenant")).loaded_skills == loaded()


def test_manifest_bounds_duplicates_and_stable_source_identity():
    with pytest.raises(ValidationError):
        agent(skills=(skill(), skill()))
    with pytest.raises(ValidationError):
        skill(instructions="x" * 16385)
    with pytest.raises(ValidationError):
        SkillSelection(names=("report",) * 5)
    assert (
        skill(required_permissions={"a", "b"}).sha256
        == skill(required_permissions={"b", "a"}).sha256
    )
    assert skill().sha256 != skill(version="1.0.1").sha256


async def test_custom_context_builder_cannot_silently_omit_skills():
    class CustomBuilder:
        name = "custom"

        async def build(self, state, definition):
            return []

    runtime = make_runtime(agent=agent(context_builder="custom"))
    runtime.contexts.register(CustomBuilder())
    with pytest.raises(ConfigurationError, match="built-in"):
        await runtime.start(request())


async def test_failed_checkpoint_keeps_previous_selection_and_can_replay():
    response = tool_call_response(SKILL_LOAD_TOOL, {"names": ["report"]})
    call = response.tool_calls[0]
    runtime = make_runtime(agent=agent())
    current = RunState(request=request(), status=RunStatus.PLANNING, pending_tool_calls=[call])
    current.append_message(response.message.model_copy(update={"tool_calls": (call,)}))
    await runtime.runs.create(current)
    save = runtime.runs.save

    async def fail_save(state, version):
        if state.loaded_skills.skills:
            raise RuntimeError("storage unavailable")
        await save(state, version)

    runtime.runs.save = fail_save
    action = PlannedAction(
        id=f"tool-{call.id}",
        type=ActionType.TOOL,
        description="Load",
        tool_name=SKILL_LOAD_TOOL,
        arguments=call.arguments,
    )
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await runtime._execute_tool(current, agent(), action)
    checkpoint = await runtime.status(current.id, "tenant")
    assert not checkpoint.loaded_skills.skills
    assert call.id not in checkpoint.completed_tool_call_ids
    assert not [
        e for e in await runtime.events.history(current.id) if e.type == "context.skills.updated"
    ]
    runtime.runs.save = save
    checkpoint.status = RunStatus.PLANNING
    await runtime._execute_tool(checkpoint, agent(), action)
    assert (await runtime.status(current.id, "tenant")).loaded_skills == loaded()


async def test_skill_permissions_are_checked_again_on_replay():
    definition = agent(
        skills=(skill(required_permissions={"reports.read"}),),
        tool_permissions={"skills.read", "reports.read"},
    )
    runtime = make_runtime(agent=definition)
    _, context = await create_tool_run(runtime)
    context = context.model_copy(update={"permissions": frozenset({"skills.read", "reports.read"})})
    await runtime.tool_executor.execute(SKILL_LOAD_TOOL, {"names": ["report"]}, context)
    with pytest.raises(PolicyDeniedError):
        await runtime.tool_executor.execute(
            SKILL_LOAD_TOOL,
            {"names": ["report"]},
            context.model_copy(update={"permissions": frozenset({"skills.read"})}),
        )


async def test_new_run_in_same_session_starts_without_loaded_skills():
    provider = MockModelProvider(
        [tool_call_response(SKILL_LOAD_TOOL, {"names": ["report"]}), "Done", "Next"]
    )
    runtime = make_runtime(provider, agent())
    first = await runtime.run(request(session_id="shared"))
    second = await runtime.run(request(session_id="shared"))
    assert (await runtime.status(first.run_id, "tenant")).loaded_skills == loaded()
    assert not (await runtime.status(second.run_id, "tenant")).loaded_skills.skills
    assert not messages(provider.requests[-1], LOADED_SKILLS_NAME)

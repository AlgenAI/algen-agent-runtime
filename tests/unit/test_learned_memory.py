from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.config.settings import AppSettings, MaintenanceSettings
from algen_agent_runtime.exceptions.errors import ConfigurationError, PolicyDeniedError
from algen_agent_runtime.memory.contracts import MemoryScope
from algen_agent_runtime.memory.service import MEMORY_DELETE, MEMORY_RECALL, MEMORY_WRITE
from algen_agent_runtime.memory.store import InMemoryLearnedMemoryStore, PostgresLearnedMemoryStore
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.orchestration.maintenance import MaintenanceService
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.planning.contracts import ActionType, PlannedAction
from algen_agent_runtime.policies.contracts import PolicyAction, PolicyDecision
from algen_agent_runtime.tools.contracts import ToolContext
from algen_agent_runtime.types.contracts import (
    ApprovalPolicy,
    LearnedMemoryPolicy,
    RunRequest,
    RunState,
    RunStatus,
    utc_now,
)


def agent(**updates):
    return make_agent(
        **{
            "enabled_tools": {MEMORY_WRITE, MEMORY_RECALL, MEMORY_DELETE},
            "tool_permissions": {"memory.write", "memory.read", "memory.delete"},
            "learned_memory_policy": LearnedMemoryPolicy(enabled=True),
            "approval_policy": ApprovalPolicy(
                require_for_side_effects=False, require_for_destructive=False
            ),
            **updates,
        }
    )


def request(**updates):
    return RunRequest(
        **{
            "agent": "test-agent",
            "input": "Remember report conventions",
            "tenant_id": "tenant",
            "user_id": "user",
            **updates,
        }
    )


def write(revision=0, **updates):
    return {
        "key": "report-format",
        "expected_revision": revision,
        "kind": "preference",
        "content": "Use concise tables",
        "evidence": "The user requested concise tables",
        "retention_seconds": 86400,
        **updates,
    }


def scope(**updates):
    return MemoryScope(
        **{"tenant_id": "tenant", "user_id": "user", "agent_id": "test-agent", **updates}
    )


async def seeded_runtime():
    store = InMemoryLearnedMemoryStore()
    runtime = make_runtime(
        MockModelProvider([tool_call_response(MEMORY_WRITE, write()), "Saved"]),
        agent(),
        learned_memory_store=store,
    )
    result = await runtime.run(request())
    assert result.status == RunStatus.COMPLETED, result.error
    return runtime, store, result


async def context_for(runtime, **updates):
    state = RunState(request=request(**updates))
    await runtime.runs.create(state)
    return ToolContext(
        run_id=state.id,
        step_id="recall",
        tenant_id=state.request.tenant_id,
        user_id=state.request.user_id,
        permissions={"memory.read", "memory.write", "memory.delete"},
        idempotency_key=f"{state.id}:recall",
    )


async def test_explicit_write_and_cross_run_recall_preserve_provenance():
    runtime, store, result = await seeded_runtime()
    record = await store.get(scope(), "report-format")
    assert record.content == "Use concise tables"
    assert record.source_run_id == result.run_id and record.source_step_id.startswith("tool-")
    assert record.source_agent_version == "1.0.0"
    assert record.expires_at is not None
    provider = MockModelProvider(
        [tool_call_response(MEMORY_RECALL, {"query": "concise"}), "Remembered"]
    )
    next_runtime = make_runtime(provider, agent(), learned_memory_store=store)
    second = await next_runtime.run(request())
    assert second.status == RunStatus.COMPLETED, second.error
    assert "Use concise tables" not in str(provider.requests[0])
    assert "Use concise tables" in str(provider.requests[1])
    events = await runtime.events.history(result.run_id)
    assert next(e for e in events if e.type == "memory.learned.updated").data == {
        "revision": 1,
        "deleted": False,
    }


@pytest.mark.parametrize(
    "update", [{"tenant_id": "other"}, {"user_id": "other"}, {"agent": "other-agent"}]
)
async def test_recall_is_scoped_to_tenant_user_and_agent(update):
    runtime, _, _ = await seeded_runtime()
    runtime.agents.register(agent(name="other-agent"))
    context = await context_for(runtime, **update)
    result = await runtime.tool_executor.execute(MEMORY_RECALL, {"query": ""}, context)
    assert result.value["records"] == []


@pytest.mark.parametrize(
    "change", [{"user_id": "wrong"}, {"tenant_id": "wrong"}, {"run_id": "missing"}]
)
async def test_direct_calls_reject_forged_scope(change):
    runtime, _, _ = await seeded_runtime()
    context = (await context_for(runtime)).model_copy(update=change)
    with pytest.raises(PolicyDeniedError):
        await runtime.tools.get(MEMORY_RECALL).execute({"key": "report-format"}, context)


async def test_completed_recall_cannot_be_replayed_into_another_valid_run():
    runtime, _, _ = await seeded_runtime()
    first = await context_for(runtime)
    await runtime.tool_executor.execute(MEMORY_RECALL, {"key": "report-format"}, first)
    other = await context_for(runtime)
    other = other.model_copy(update={"idempotency_key": first.idempotency_key})
    with pytest.raises(PolicyDeniedError, match="execution scope"):
        await runtime.tool_executor.execute(MEMORY_RECALL, {"key": "report-format"}, other)


async def test_correction_deletion_and_stale_recall():
    runtime, store, _ = await seeded_runtime()
    context = await context_for(runtime)
    await runtime.tool_executor.execute(MEMORY_RECALL, {"key": "report-format"}, context)
    writer = make_runtime(
        MockModelProvider(
            [
                tool_call_response(MEMORY_WRITE, write(1, content="Use paragraphs")),
                tool_call_response(MEMORY_DELETE, {"key": "report-format", "expected_revision": 2}),
                "Deleted",
            ]
        ),
        agent(),
        learned_memory_store=store,
    )
    result = await writer.run(request())
    assert result.status == RunStatus.COMPLETED, result.error
    with pytest.raises(PolicyDeniedError, match="changed"):
        await runtime.tool_executor.execute(MEMORY_RECALL, {"key": "report-format"}, context)
    fresh = await runtime.tool_executor.execute(
        MEMORY_RECALL, {"key": "report-format"}, await context_for(runtime)
    )
    assert fresh.value["records"][0]["revision"] == 3
    assert fresh.value["records"][0]["content"] is None
    assert fresh.value["records"][0]["deleted"]


async def test_expired_memory_is_hidden_replay_denied_and_maintenance_purges():
    runtime, store, _ = await seeded_runtime()
    context = await context_for(runtime)
    await runtime.tool_executor.execute(MEMORY_RECALL, {"key": "report-format"}, context)
    key = ("tenant", "user", "test-agent", "report-format")
    store._records[key] = store._records[key].model_copy(
        update={"expires_at": utc_now() - timedelta(seconds=1)}
    )
    with pytest.raises(PolicyDeniedError):
        await runtime.tool_executor.execute(MEMORY_RECALL, {"key": "report-format"}, context)
    result = await runtime.tools.get(MEMORY_RECALL).execute(
        {"key": "report-format"}, await context_for(runtime)
    )
    assert result["records"][0]["content"] is None
    report = await MaintenanceService(
        runtime, InMemoryArtifactStore(), MaintenanceSettings()
    ).run_once()
    assert report.purged_learned_memory == 1
    assert (await store.get(scope(), "report-format")).evidence is None


class MemoryPolicy:
    def __init__(self, mode, point):
        self.mode, self.point = mode, point

    async def evaluate(self, point, payload, context):
        if point == self.point and (
            point != "before_memory_write" or context.get("operation") == MEMORY_WRITE
        ):
            if self.mode == "deny":
                return PolicyDecision(
                    action=PolicyAction.DENY, reason="Denied", reason_code="test.denied"
                )
            value = dict(payload)
            value["content" if self.mode == "redact" else "key"] = "redacted"
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
@pytest.mark.parametrize("mode", ["deny", "redact", "redirect"])
async def test_policies_run_before_memory_side_effect(point, mode):
    store = InMemoryLearnedMemoryStore()
    runtime = make_runtime(
        MockModelProvider([tool_call_response(MEMORY_WRITE, write()), "Done"]),
        agent(),
        learned_memory_store=store,
    )
    policy = MemoryPolicy(mode, point)
    runtime.policies = policy
    runtime.tool_executor._policy_engine = policy
    result = await runtime.run(request())
    record = await store.get(scope(), "report-format")
    if mode == "redact":
        assert result.status == RunStatus.COMPLETED, result.error
        assert record.content == "redacted"
    else:
        assert result.status == RunStatus.FAILED
        assert record is None
        assert await store.get(scope(), "redacted") is None


async def test_default_approval_precedes_write():
    store = InMemoryLearnedMemoryStore()
    runtime = make_runtime(
        MockModelProvider([tool_call_response(MEMORY_WRITE, write()), "Done"]),
        agent(approval_policy=ApprovalPolicy()),
        learned_memory_store=store,
    )
    result = await runtime.run(request())
    assert result.status == RunStatus.AWAITING_APPROVAL
    assert await store.get(scope(), "report-format") is None
    await runtime.resume(result.run_id, "tenant", {"decision": "approved"})
    assert (await runtime.wait(result.run_id, "tenant")).status == RunStatus.COMPLETED
    assert (await store.get(scope(), "report-format")).revision == 1


async def test_committed_store_write_replays_after_run_checkpoint_failure():
    store = InMemoryLearnedMemoryStore()
    runtime = make_runtime(agent=agent(), learned_memory_store=store)
    call = tool_call_response(MEMORY_WRITE, write()).tool_calls[0]
    from algen_agent_runtime.types.contracts import Message, Role

    current = RunState(request=request(), status=RunStatus.PLANNING, pending_tool_calls=[call])
    current.append_message(Message(role=Role.ASSISTANT, content=(), tool_calls=(call,)))
    await runtime.runs.create(current)
    save = runtime.runs.save

    async def fail_save(state, version):
        if state.completed_tool_call_ids:
            raise RuntimeError("checkpoint unavailable")
        await save(state, version)

    runtime.runs.save = fail_save
    action = PlannedAction(
        id=f"tool-{call.id}",
        type=ActionType.TOOL,
        description="Remember",
        tool_name=MEMORY_WRITE,
        arguments=call.arguments,
    )
    with pytest.raises(RuntimeError):
        await runtime._execute_tool(current, agent(), action)
    assert (await store.get(scope(), "report-format")).revision == 1
    checkpoint = await runtime.status(current.id, "tenant")
    assert not checkpoint.completed_tool_call_ids
    runtime.runs.save = save
    checkpoint.status = RunStatus.PLANNING
    await runtime._execute_tool(checkpoint, agent(), action)
    assert (await store.get(scope(), "report-format")).revision == 1
    assert call.id in (await runtime.status(current.id, "tenant")).completed_tool_call_ids


@pytest.mark.parametrize(
    "definition,has_store",
    [
        (agent(), False),
        (agent(tool_permissions=set()), True),
        (agent(learned_memory_policy=LearnedMemoryPolicy()), True),
    ],
)
async def test_feature_requires_store_enablement_permissions(definition, has_store):
    runtime = make_runtime(
        agent=definition, learned_memory_store=InMemoryLearnedMemoryStore() if has_store else None
    )
    with pytest.raises(ConfigurationError, match="memory"):
        await runtime.start(request())


async def test_retention_policy_and_recall_budget():
    store = InMemoryLearnedMemoryStore()
    runtime = make_runtime(
        MockModelProvider([tool_call_response(MEMORY_WRITE, write())]),
        agent(learned_memory_policy=LearnedMemoryPolicy(enabled=True, max_retention_seconds=60)),
        learned_memory_store=store,
    )
    result = await runtime.run(request())
    assert result.status == RunStatus.FAILED and await store.get(scope(), "report-format") is None
    runtime, store, _ = await seeded_runtime()
    provider = MockModelProvider([tool_call_response(MEMORY_RECALL, {"key": "report-format"})])
    reader = make_runtime(
        provider,
        agent(learned_memory_policy=LearnedMemoryPolicy(enabled=True, max_recall_tokens=128)),
        learned_memory_store=store,
    )
    result = await reader.run(request())
    assert result.status == RunStatus.FAILED


def test_container_selects_postgres_without_importing_driver(monkeypatch):
    import sys

    monkeypatch.setenv("TEST_MEMORY_POSTGRES_DSN", "postgresql://localhost/test")
    monkeypatch.setitem(sys.modules, "asyncpg", None)
    container = build_container(
        AppSettings.model_validate(
            {
                "storage": {
                    "learned_memory_store": "postgres",
                    "postgres_dsn": "env://TEST_MEMORY_POSTGRES_DSN",
                }
            }
        )
    )
    assert isinstance(container.runtime.learned_memory.store, PostgresLearnedMemoryStore)
    container.close()


async def test_delete_waits_for_destructive_approval_and_rejection_keeps_content():
    _, store, _ = await seeded_runtime()
    definition = agent(approval_policy=ApprovalPolicy(require_for_side_effects=False))
    runtime = make_runtime(
        MockModelProvider(
            [tool_call_response(MEMORY_DELETE, {"key": "report-format", "expected_revision": 1})]
        ),
        definition,
        learned_memory_store=store,
    )
    result = await runtime.run(request())
    assert result.status == RunStatus.AWAITING_APPROVAL
    assert (await store.get(scope(), "report-format")).content == "Use concise tables"
    await runtime.resume(result.run_id, "tenant", {"decision": "rejected"})
    assert (await store.get(scope(), "report-format")).revision == 1


async def test_recall_replay_rechecks_expiry_without_record_change(monkeypatch):
    runtime, store, _ = await seeded_runtime()
    context = await context_for(runtime)
    await runtime.tool_executor.execute(MEMORY_RECALL, {"key": "report-format"}, context)
    expiry = (await store.get(scope(), "report-format")).expires_at
    monkeypatch.setattr(
        "algen_agent_runtime.memory.service.utc_now", lambda: expiry + timedelta(seconds=1)
    )
    with pytest.raises(PolicyDeniedError, match="expired"):
        await runtime.tool_executor.execute(MEMORY_RECALL, {"key": "report-format"}, context)


async def test_search_reports_budget_omissions_without_partial_content():
    _, store, _ = await seeded_runtime()
    key = ("tenant", "user", "test-agent", "report-format")
    store._records[key] = store._records[key].model_copy(update={"content": "x" * 2048})
    reader = make_runtime(
        agent=agent(learned_memory_policy=LearnedMemoryPolicy(enabled=True, max_recall_tokens=256)),
        learned_memory_store=store,
    )
    result = await reader.tool_executor.execute(
        MEMORY_RECALL, {"query": ""}, await context_for(reader)
    )
    assert result.value["truncated"] is True
    assert result.value["records"] == []


async def test_recall_binding_cannot_accidentally_match_pii_numbers(monkeypatch):
    from algen_agent_runtime.memory.service import LearnedMemoryService
    from algen_agent_runtime.policies.engine import PIIRedactionPolicy

    class Digest:
        def hexdigest(self):
            return "2" * 64

    monkeypatch.setattr("algen_agent_runtime.memory.service.hashlib.sha256", lambda value: Digest())
    context = ToolContext(
        run_id="run",
        step_id="step",
        tenant_id="tenant",
        user_id="user",
        permissions={"memory.read"},
        idempotency_key="key",
    )
    binding = LearnedMemoryService._binding(scope(), context)
    assert binding == "c" * 64
    decision = await PIIRedactionPolicy().evaluate("after_tool", {"binding": binding}, {})
    assert decision.action == PolicyAction.ALLOW

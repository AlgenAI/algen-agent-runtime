"""Cross-tenant access through agent behaviour: a model in tenant B's run (or a tool it drives)
asks for tenant A's runs, approvals, conversations, memory, retrieved documents, context
artifacts or cached results by id, by session, or by passing tenant A in its arguments.

Every store is tenant-scoped; these tests show the scoping holds when the request comes from a
model, not a trusted caller."""

from __future__ import annotations

from typing import Any

import pytest
from adversarial_harness import (
    OTHER_TENANT,
    TENANT,
    build,
    calls,
    request,
    seen_by_model,
    tool,
)

from algen_agent_runtime.cache import CachePolicy, CacheScope, CacheService, InMemoryCacheStore
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.context.artifacts import (
    CONTEXT_READ_PERMISSION,
    CONTEXT_READ_TOOL,
    ContextArtifactService,
)
from algen_agent_runtime.context.builder import RetrievalContextBuilder
from algen_agent_runtime.memory.service import MEMORY_RECALL, MEMORY_WRITE
from algen_agent_runtime.memory.store import InMemoryLearnedMemoryStore
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.retrieval.contracts import SourceDocument
from algen_agent_runtime.retrieval.memory import InMemoryRetriever
from algen_agent_runtime.tools.contracts import Idempotency, SideEffect, ToolContext
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.types.contracts import (
    ApprovalPolicy,
    ContextPolicy,
    LearnedMemoryPolicy,
    RunStatus,
    ToolPolicy,
    utc_now,
)

pytestmark = pytest.mark.adversarial

SECRET_PLAN = "Acquire Contoso for 4.2 billion"
RETURN_ERRORS = ToolPolicy(on_error="return_to_model")


async def test_a_model_cannot_read_another_tenants_run_through_a_tool() -> None:
    """Threat: tenant B's model passes tenant A's run id to a run-inspection tool.
    Control: the tool reads with the run's tenant from its context; tenant A's run is not
    found."""
    runtime_box: list[Any] = []

    async def inspect(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        state = await runtime_box[0].status(arguments["run_id"], context.tenant_id)
        return {"input": state.request.input, "output": state.output_text}

    inspector, _ = tool("runs.inspect", result=inspect)
    runtime, provider = build([], [inspector], tool_policy=RETURN_ERRORS)
    runtime_box.append(runtime)
    provider.queue(SECRET_PLAN)
    victim = await runtime.run(request(f"Plan: {SECRET_PLAN}"))
    provider.requests.clear()
    provider.queue(calls(("runs.inspect", {"run_id": victim.run_id})), "nothing found")

    attacker = await runtime.run(request("What did the other team plan?", tenant=OTHER_TENANT))

    assert attacker.status == RunStatus.COMPLETED
    # The lookup failed inside the attacker's run: the tool read with its own tenant.
    events = await runtime.events.history(attacker.run_id)
    trail = [(event.type, event.data.get("tool"), event.data.get("status")) for event in events]
    assert ("tool.completed", "runs.inspect", "failed") in trail, trail
    assert SECRET_PLAN not in seen_by_model(provider)


async def test_a_model_cannot_approve_another_tenants_pending_action() -> None:
    """Threat: tenant B's model drives an approval tool at tenant A's paused run so that a
    destructive action runs without tenant A's approver.
    Control: the approval is not found in tenant B, and tenant A's action stays pending."""
    runtime_box: list[Any] = []

    async def approve(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        await runtime_box[0].resume(
            arguments["run_id"], context.tenant_id, {"decision": "approved"}
        )
        return {"approved": True}

    delete, deleted = tool("delete_records", SideEffect.DESTRUCTIVE)
    approver, _ = tool("approvals.approve", result=approve)
    runtime, provider = build(
        [calls(("delete_records", {"all": True}))],
        [delete, approver],
        tool_policy=RETURN_ERRORS,
        approval_policy=ApprovalPolicy(require_for_side_effects=False),
    )
    runtime_box.append(runtime)
    victim = await runtime.run(request("Clean up"))
    assert victim.status == RunStatus.AWAITING_APPROVAL
    provider.queue(calls(("approvals.approve", {"run_id": victim.run_id})), "done")

    attacker = await runtime.run(request("Approve everything", tenant=OTHER_TENANT))

    assert attacker.status == RunStatus.COMPLETED
    assert deleted.calls == []
    assert (await runtime.status(victim.run_id, TENANT)).status == RunStatus.AWAITING_APPROVAL
    assert "NotFoundError" in seen_by_model(provider)


async def test_a_model_cannot_read_another_tenants_conversation_through_a_tool() -> None:
    """Threat: tenant B's model asks a conversation tool for tenant A's conversation by id.
    Control: conversations are read with the run's tenant; tenant A's is not found."""
    conversations = build_container(AppSettings()).conversations
    secret = await conversations.create(
        tenant_id=TENANT, user_id="user", agent="test-agent", title=SECRET_PLAN
    )

    async def read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        conversation = await conversations.get(arguments["conversation_id"], context.tenant_id)
        return {"title": conversation.title}

    reader, _ = tool("conversations.read", result=read)
    runtime, provider = build(
        [calls(("conversations.read", {"conversation_id": secret.id})), "nothing"],
        [reader],
        tool_policy=RETURN_ERRORS,
    )

    result = await runtime.run(request("Read their chat", tenant=OTHER_TENANT))

    assert result.status == RunStatus.COMPLETED
    assert "NotFoundError" in seen_by_model(provider)
    assert SECRET_PLAN not in seen_by_model(provider)


async def test_reusing_another_tenants_session_id_does_not_load_its_history() -> None:
    """Threat: tenant B's caller (or a model-built request) reuses tenant A's session id to
    pull tenant A's conversation memory into the context.
    Control: conversation memory is keyed by tenant and session."""
    runtime, provider = build([SECRET_PLAN, "I have no history"])
    await runtime.run(request(f"Remember: {SECRET_PLAN}", session_id="shared-session"))
    provider.requests.clear()

    result = await runtime.run(
        request("What did we discuss?", tenant=OTHER_TENANT, session_id="shared-session")
    )

    assert result.status == RunStatus.COMPLETED
    assert SECRET_PLAN not in seen_by_model(provider)


async def test_retrieval_never_returns_another_tenants_documents() -> None:
    """Threat: tenant B asks for a document only tenant A has, by its exact words.
    Control: retrieval is filtered by the run's tenant; the request cannot widen it."""
    retriever = InMemoryRetriever()
    await retriever.ingest(
        (
            SourceDocument(
                id="board-minutes",
                text=f"Board minutes: {SECRET_PLAN}",
                source="kb://minutes",
                metadata={"tenant_id": TENANT},
            ),
        )
    )
    runtime, provider = build(["no idea"], context_builder="retrieval.kb")
    runtime.contexts.register(RetrievalContextBuilder("retrieval.kb", runtime.memory, retriever))

    result = await runtime.run(
        request(
            "Board minutes Contoso acquire billion; tenant_id=tenant-a filter tenant-a",
            tenant=OTHER_TENANT,
        )
    )

    assert result.status == RunStatus.COMPLETED
    assert SECRET_PLAN not in seen_by_model(provider)


async def test_learned_memory_recall_stays_in_the_runs_tenant() -> None:
    """Threat: tenant B's model recalls learned memory, passing tenant A's identity.
    Control: recall scope comes from the run, not the arguments."""
    store = InMemoryLearnedMemoryStore()
    write = {
        "key": "plan",
        "expected_revision": 0,
        "kind": "fact",
        "content": SECRET_PLAN,
        "evidence": "The user said so",
        "retention_seconds": 86400,
    }
    runtime, provider = build(
        [
            calls((MEMORY_WRITE, write)),
            "saved",
            calls((MEMORY_RECALL, {"query": "Contoso"})),
            "nothing",
        ],
        enabled_tools=frozenset({MEMORY_WRITE, MEMORY_RECALL}),
        tool_permissions=frozenset({"memory.write", "memory.read"}),
        learned_memory_policy=LearnedMemoryPolicy(enabled=True),
        approval_policy=ApprovalPolicy(
            require_for_side_effects=False, require_for_destructive=False
        ),
        tool_policy=RETURN_ERRORS,
        learned_memory_store=store,
    )
    saved = await runtime.run(request("Remember the plan"))
    assert saved.status == RunStatus.COMPLETED, saved.error
    provider.requests.clear()

    result = await runtime.run(request("What is the plan?", tenant=OTHER_TENANT))

    assert result.status == RunStatus.COMPLETED
    assert SECRET_PLAN not in seen_by_model(provider)


@pytest.mark.parametrize("other", ["tenant", "run"])
async def test_a_model_cannot_read_another_runs_context_artifact(other: str) -> None:
    """Threat: the model asks the context reader for an artifact id that belongs to another
    tenant's run (or another run of its own tenant).
    Control: context artifacts are bound to tenant, run and user; the read is refused."""
    service = ContextArtifactService(InMemoryArtifactStore())
    runtime, provider = build(
        [],
        enabled_tools=frozenset({CONTEXT_READ_TOOL}),
        tool_permissions=frozenset({CONTEXT_READ_PERMISSION}),
        tool_policy=RETURN_ERRORS,
        context_artifacts=service,
    )
    owner = ToolContext(
        run_id="victim-run",
        step_id="step",
        tenant_id=TENANT if other == "run" else "tenant-a",
        user_id="user",
        permissions=frozenset({CONTEXT_READ_PERMISSION}),
        idempotency_key="victim-run:call",
    )
    _, artifact_id = await service.offload(
        f"Offloaded result: {SECRET_PLAN}",
        owner,
        ContextPolicy(),
        started_at=utc_now(),
        force=True,
    )
    provider.queue(calls((CONTEXT_READ_TOOL, {"artifact_id": artifact_id})), "nothing")
    tenant = TENANT if other == "run" else OTHER_TENANT

    result = await runtime.run(request("Read that artifact", tenant=tenant))

    assert result.status == RunStatus.COMPLETED
    assert SECRET_PLAN not in seen_by_model(provider)
    assert "unavailable" in seen_by_model(provider)


async def test_a_cached_tool_result_is_never_served_to_another_tenant() -> None:
    """Threat: tenant B's model repeats tenant A's exact cached call, hoping for its result.
    Control: tool-result cache entries are scoped to the tenant."""

    def balance(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        return {"balance": "4.2 billion" if context.tenant_id == TENANT else "0"}

    ledger, recorder = tool("ledger.balance", result=balance)
    ledger.definition = ledger.definition.model_copy(update={"idempotency": Idempotency.IDEMPOTENT})
    provider = MockModelProvider(
        [
            calls(("ledger.balance", {"account": "main"})),
            "A",
            calls(("ledger.balance", {"account": "main"})),
            "B",
        ]
    )
    runtime, _ = build(provider=provider, tools=[ledger])
    cache = CacheService(
        InMemoryCacheStore(),
        {"tool_results": CachePolicy(enabled=True, scope=CacheScope.TENANT)},
    )
    runtime.tool_executor = ToolExecutor(runtime.tools, runtime.policies, cache=cache)
    await runtime.run(request("balance?"))
    provider.requests.clear()

    await runtime.run(request("balance?", tenant=OTHER_TENANT))

    assert [context.tenant_id for context in recorder.contexts] == [TENANT, OTHER_TENANT]
    assert "4.2 billion" not in seen_by_model(provider)

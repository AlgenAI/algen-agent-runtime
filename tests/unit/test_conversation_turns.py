"""Conversation turns that stream, pause for approval or input, and resume (issue #50)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from conftest import make_agent

from algen_agent_runtime.approvals.service import InMemoryApprovalService
from algen_agent_runtime.config.registry import InMemoryAgentRegistry
from algen_agent_runtime.config.settings import MaintenanceSettings
from algen_agent_runtime.context.builder import ContextBuilderRegistry, DefaultContextBuilder
from algen_agent_runtime.conversations import (
    ConversationApprovalDecision,
    ConversationCredentialsRenewal,
    ConversationHandlerRegistry,
    ConversationInputAnswer,
    ConversationMessage,
    ConversationService,
    MessageStatus,
    PendingTurnExpiredError,
    RuntimeConversationHandler,
)
from algen_agent_runtime.conversations.presentation import (
    RUN_FAILURE_MESSAGES,
    ConversationPresentation,
    PresentationAudience,
)
from algen_agent_runtime.conversations.stores import (
    InMemoryConversationEventBus,
    InMemoryConversationStore,
)
from algen_agent_runtime.conversations.streaming import DeltaCoalescer
from algen_agent_runtime.events.bus import InMemoryAuditLog, InMemoryEventBus
from algen_agent_runtime.exceptions.errors import (
    ConfigurationError,
    ConflictError,
    MeterRefusedError,
    NotFoundError,
)
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.maintenance import MaintenanceService
from algen_agent_runtime.persistence.memory import (
    InMemoryArtifactStore,
    InMemoryMemoryStore,
    InMemoryRunStore,
)
from algen_agent_runtime.planning.contracts import ActionType, Plan, PlannedAction
from algen_agent_runtime.planning.planners import PlannerRegistry, ReActPlanner
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.responses.composer import ResponseComposerRegistry
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.types.contracts import (
    AgentDefinition,
    ModelCapabilities,
    RunState,
    RunStatus,
    utc_now,
)
from algen_agent_runtime.verification.verifiers import VerificationService

SECRET = "cred-50c0ffee-do-not-leak"
NEWER = "cred-50beef00-after-resume"
TENANT = "tenant-a"
USER = "user-a"
SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "additionalProperties": False,
}


class Seen:
    """What each tool call received."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def tool(self, name: str, side_effect: SideEffect) -> Tool:
        async def handler(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
            self.calls.append((name, dict(arguments), dict(context.credentials)))
            return {"ok": True}

        return Tool(
            ToolDefinition(
                name=name,
                version="1",
                description=f"{name} for tests.",
                input_schema=SCHEMA,
                output_schema={"type": "object"},
                side_effect=side_effect,
                idempotency="keyed",
            ),
            handler,
        )


class AskFirstPlanner:
    """Asks the user one question before planning as ReAct does."""

    name = "ask_first"

    async def plan(self, state: RunState, agent: AgentDefinition) -> Plan:
        if state.step_count == 0:
            return Plan(
                actions=(
                    PlannedAction(
                        id="clarify-1", type=ActionType.CLARIFY, description="Which account?"
                    ),
                ),
                decision_summary="The request names no account.",
            )
        return await ReActPlanner().plan(state, agent)


class NonStreamingProvider(MockModelProvider):
    async def capabilities(self, model: str) -> ModelCapabilities:
        return (await super().capabilities(model)).model_copy(update={"streaming": False})


@dataclass
class Stores:
    """What survives a restart: run, event, approval, memory and conversation stores."""

    runs: InMemoryRunStore = field(default_factory=InMemoryRunStore)
    events: InMemoryEventBus = field(default_factory=InMemoryEventBus)
    approvals: InMemoryApprovalService = field(default_factory=InMemoryApprovalService)
    memory: InMemoryMemoryStore = field(default_factory=InMemoryMemoryStore)
    conversations: InMemoryConversationStore = field(default_factory=InMemoryConversationStore)
    conversation_events: InMemoryConversationEventBus = field(
        default_factory=InMemoryConversationEventBus
    )
    audits: InMemoryAuditLog = field(default_factory=InMemoryAuditLog)


def build(
    provider: MockModelProvider,
    seen: Seen,
    stores: Stores | None = None,
    *,
    handler_options: dict[str, Any] | None = None,
    usage_meter: Any = None,
    extra_agents: tuple[str, ...] = (),
    **agent: Any,
) -> tuple[ConversationService, AgentRuntime, Stores]:
    """A runtime and a conversation service over ``stores``; call again to 'restart'."""
    stores = stores or Stores()
    agents = InMemoryAgentRegistry()
    agents.register(
        make_agent(enabled_tools=frozenset({"api.read", "api.write"}), max_steps=12, **agent)
    )
    for name in extra_agents:
        agents.register(make_agent(name=name, enabled_tools=frozenset({"api.read"})))
    tools = ToolRegistry()
    tools.register(seen.tool("api.read", SideEffect.READ))
    tools.register(seen.tool("api.write", SideEffect.WRITE))
    policies = CompositePolicyEngine()
    router = ModelRouter(circuit_failure_threshold=1, usage_meter=usage_meter)
    router.register_provider(provider)
    planners = PlannerRegistry()
    planners.register(AskFirstPlanner())
    runtime = AgentRuntime(
        agents=agents,
        router=router,
        tools=tools,
        tool_executor=ToolExecutor(tools, policies),
        planners=planners,
        contexts=ContextBuilderRegistry(DefaultContextBuilder(stores.memory)),
        policies=policies,
        verifiers=VerificationService(),
        composers=ResponseComposerRegistry(),
        runs=stores.runs,
        memory=stores.memory,
        events=stores.events,
        approvals=stores.approvals,
        audits=stores.audits,
    )
    handlers = ConversationHandlerRegistry()
    handlers.register(RuntimeConversationHandler(runtime, **(handler_options or {})))
    service = ConversationService(
        stores.conversations, stores.conversation_events, handlers, stores.audits
    )
    return service, runtime, stores


async def start(service: ConversationService) -> str:
    conversation = await service.create(tenant_id=TENANT, user_id=USER, agent="test-agent")
    return conversation.id


async def settled(
    service: ConversationService, conversation_id: str, *statuses: MessageStatus
) -> ConversationMessage:
    wanted = set(statuses) or {
        MessageStatus.COMPLETED,
        MessageStatus.FAILED,
        MessageStatus.CANCELLED,
        MessageStatus.EXPIRED,
        MessageStatus.AWAITING_APPROVAL,
        MessageStatus.AWAITING_INPUT,
    }
    for _ in range(400):
        messages = await service.messages(conversation_id, TENANT)
        if messages and messages[-1].status in wanted:
            return messages[-1]
        await asyncio.sleep(0.005)
    raise AssertionError(f"message never reached {wanted}: {messages[-1].status}")


async def events(service: ConversationService, conversation_id: str) -> list[Any]:
    return list(await service.events.history(conversation_id))


def types(items: Iterable[Any]) -> list[str]:
    return [item.type for item in items]


async def paused_for_approval(
    *responses: Any, credentials: dict[str, str] | None = None, **agent: Any
) -> tuple[ConversationService, AgentRuntime, Stores, Seen, str, ConversationMessage]:
    seen = Seen()
    provider = MockModelProvider(list(responses))
    service, runtime, stores = build(provider, seen, **agent)
    conversation_id = await start(service)
    await service.submit(conversation_id, TENANT, USER, "update it", credentials=credentials)
    message = await settled(service, conversation_id)
    assert message.status == MessageStatus.AWAITING_APPROVAL, message
    return service, runtime, stores, seen, conversation_id, message


# --- streaming ------------------------------------------------------------------------------------


async def test_streamed_turn_emits_ordered_deltas_then_one_completed_message() -> None:
    service, runtime, _ = build(
        MockModelProvider(["one two three four"]),
        Seen(),
        handler_options={"delta_interval_seconds": 0},
    )
    conversation_id = await start(service)
    _, assistant = await service.submit(conversation_id, TENANT, USER, "count")
    message = await settled(service, conversation_id)

    history = await events(service, conversation_id)
    deltas = [event for event in history if event.type == "assistant.message.delta"]
    assert message.status == MessageStatus.COMPLETED
    assert message.text_content == "one two three four"
    assert [event.data["delta"] for event in deltas] == ["one ", "two ", "three ", "four "]
    assert all(event.data["message_id"] == assistant.id for event in deltas)
    assert all(event.message_id == assistant.id for event in deltas)
    sequences = [event.sequence for event in history]
    assert sequences == sorted(sequences)
    assert types(history).count("assistant.message.completed") == 1
    assert types(history)[-1] == "assistant.message.completed"
    assert types(history).index("assistant.message.started") < history.index(deltas[0])
    run = await runtime.status(message.run_ids[0], TENANT)
    assert run.request.stream is True
    # Deltas are not audited: the completed message is the record of the turn.
    audits = await service._audits.list(TENANT)  # type: ignore[union-attr]
    assert "assistant.message.delta" not in {audit.action for audit in audits}


async def test_small_deltas_are_coalesced_within_the_interval() -> None:
    service, _, _ = build(
        MockModelProvider(["one two three four"]),
        Seen(),
        handler_options={"delta_interval_seconds": 30},
    )
    conversation_id = await start(service)
    await service.submit(conversation_id, TENANT, USER, "count")
    await settled(service, conversation_id)

    deltas = [
        event.data["delta"]
        for event in await events(service, conversation_id)
        if event.type == "assistant.message.delta"
    ]
    assert deltas == ["one two three four "]


async def test_delta_coalescer_bounds_event_size_and_flushes_the_rest() -> None:
    sent: list[str] = []

    async def emit(event_type: str, data: dict[str, Any] | None = None) -> None:
        assert event_type == "assistant.message.delta"
        sent.append((data or {})["delta"])

    coalescer = DeltaCoalescer(emit, interval_seconds=60, max_chars=8)
    await coalescer.add("abcd")
    assert sent == []
    assert coalescer.timeout() is not None
    await coalescer.add("efghij")
    assert sent == ["abcdefgh", "ij"]
    await coalescer.add("k")
    await coalescer.flush()
    assert sent == ["abcdefgh", "ij", "k"]
    assert coalescer.timeout() is None
    with pytest.raises(ValueError):
        DeltaCoalescer(emit, interval_seconds=1, max_chars=0)


async def test_non_streaming_provider_gives_only_the_final_message() -> None:
    service, runtime, _ = build(NonStreamingProvider(["plain answer"]), Seen())
    conversation_id = await start(service)
    await service.submit(conversation_id, TENANT, USER, "hi")
    message = await settled(service, conversation_id)

    assert message.status == MessageStatus.COMPLETED
    assert message.text_content == "plain answer"
    assert "assistant.message.delta" not in types(await events(service, conversation_id))
    assert (await runtime.status(message.run_ids[0], TENANT)).request.stream is False


async def test_streaming_can_be_turned_off() -> None:
    service, _, _ = build(MockModelProvider(["quiet"]), Seen(), handler_options={"stream": False})
    conversation_id = await start(service)
    await service.submit(conversation_id, TENANT, USER, "hi")
    message = await settled(service, conversation_id)

    assert message.text_content == "quiet"
    assert "assistant.message.delta" not in types(await events(service, conversation_id))


# --- approvals ------------------------------------------------------------------------------------


async def test_turn_pauses_for_approval_and_resumes_into_the_same_message() -> None:
    service, runtime, _, seen, conversation_id, paused = await paused_for_approval(
        tool_call_response("api.write", {"value": "draft"}), "saved"
    )
    pending = paused.pending
    assert pending is not None
    assert pending.kind == "approval"
    assert pending.approval_id
    assert pending.tool == "api.write"
    assert pending.parameters_schema == SCHEMA
    assert pending.redacted_parameters == {"value": "draft"}
    assert set(pending.options) == {"approved", "rejected", "modified"}
    assert pending.turn_id == paused.reply_to_message_id
    assert seen.calls == []
    history = await events(service, conversation_id)
    awaiting = next(e for e in history if e.type == "assistant.message.awaiting_approval")
    assert awaiting.message_id == paused.id
    assert awaiting.data["pending"]["approval_id"] == pending.approval_id
    assert awaiting.data["pending"]["parameters_schema"] == SCHEMA
    assert awaiting.data["turn_id"] == pending.turn_id

    resumed = await service.respond(
        conversation_id,
        pending.turn_id,
        TENANT,
        USER,
        ConversationApprovalDecision(decision="approved"),
    )
    assert resumed.id == paused.id
    assert resumed.status == MessageStatus.STREAMING
    done = await settled(service, conversation_id, MessageStatus.COMPLETED, MessageStatus.FAILED)

    assert done.id == paused.id
    assert done.status == MessageStatus.COMPLETED
    assert done.text_content == "saved"
    assert done.pending is None
    assert done.run_ids == paused.run_ids
    assert [call[:2] for call in seen.calls] == [("api.write", {"value": "draft"})]
    messages = await service.messages(conversation_id, TENANT)
    assert len(messages) == 2
    history = types(await events(service, conversation_id))
    assert history.index("assistant.message.awaiting_approval") < history.index(
        "assistant.message.resumed"
    )
    assert history[-1] == "assistant.message.completed"
    after_resume = history[history.index("assistant.message.resumed") :]
    # The resumed drive streams into the same message.
    assert "assistant.message.delta" in after_resume
    resumed_event = next(
        e for e in await events(service, conversation_id) if e.type == "assistant.message.resumed"
    )
    assert resumed_event.data["decision"] == "approved"
    assert resumed_event.data["decided_by"] == USER
    assert (await runtime.status(done.run_ids[0], TENANT)).status == RunStatus.COMPLETED
    # The approval request records the conversation's owner as the approver.
    approval = await runtime.approvals.get(pending.approval_id, TENANT)
    assert approval is not None
    assert (approval.decided_by, approval.comment) == (USER, None)
    assert approval.decided_at is not None


async def test_modified_decision_runs_the_tool_with_the_new_parameters() -> None:
    service, _, _, seen, conversation_id, paused = await paused_for_approval(
        tool_call_response("api.write", {"value": "draft"}), "saved"
    )
    assert paused.pending is not None

    await service.respond(
        conversation_id,
        paused.pending.turn_id,
        TENANT,
        USER,
        ConversationApprovalDecision(decision="modified", modified_parameters={"value": "final"}),
    )
    done = await settled(service, conversation_id, MessageStatus.COMPLETED, MessageStatus.FAILED)

    assert done.status == MessageStatus.COMPLETED
    assert [call[:2] for call in seen.calls] == [("api.write", {"value": "final"})]


async def test_rejected_decision_ends_the_turn_with_a_clear_message() -> None:
    service, runtime, _, seen, conversation_id, paused = await paused_for_approval(
        tool_call_response("api.write", {"value": "draft"}), "saved"
    )
    assert paused.pending is not None

    await service.respond(
        conversation_id,
        paused.pending.turn_id,
        TENANT,
        USER,
        ConversationApprovalDecision(decision="rejected", reason="not now"),
    )
    done = await settled(service, conversation_id, MessageStatus.COMPLETED, MessageStatus.FAILED)
    # The rejection's reason is recorded as the approver's comment.
    approval = await runtime.approvals.get(str(paused.pending.approval_id), TENANT)
    assert approval is not None
    assert (approval.decided_by, approval.comment) == (USER, "not now")

    assert done.id == paused.id
    assert done.status == MessageStatus.FAILED
    assert done.text_content == "Proposed action was rejected."
    assert done.metadata["reason"] == "approval_rejected"
    assert seen.calls == []


async def test_rejection_can_let_the_agent_continue() -> None:
    from algen_agent_runtime.types.contracts import ApprovalPolicy

    service, _, _, seen, conversation_id, paused = await paused_for_approval(
        tool_call_response("api.write", {"value": "draft"}),
        "Understood, nothing was changed.",
        approval_policy=ApprovalPolicy(on_rejection="continue"),
    )
    assert paused.pending is not None

    await service.respond(
        conversation_id,
        paused.pending.turn_id,
        TENANT,
        USER,
        ConversationApprovalDecision(decision="rejected"),
    )
    done = await settled(service, conversation_id, MessageStatus.COMPLETED, MessageStatus.FAILED)

    assert done.status == MessageStatus.COMPLETED
    assert done.text_content == "Understood, nothing was changed."
    assert seen.calls == []


def test_decision_contract_validates_modified_parameters() -> None:
    with pytest.raises(ValueError, match="modified_parameters"):
        ConversationApprovalDecision(decision="modified")
    with pytest.raises(ValueError, match="only allowed"):
        ConversationApprovalDecision(decision="approved", modified_parameters={"value": "x"})
    with pytest.raises(ValueError):
        ConversationApprovalDecision(decision="maybe")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ConversationInputAnswer(text="   ")


async def test_answer_of_the_wrong_kind_or_an_unknown_turn_is_refused() -> None:
    service, _, _, _, conversation_id, paused = await paused_for_approval(
        tool_call_response("api.write", {"value": "draft"}), "saved"
    )
    assert paused.pending is not None
    with pytest.raises(ConflictError, match="awaits an approval"):
        await service.respond(
            conversation_id, paused.pending.turn_id, TENANT, USER, ConversationInputAnswer(text="x")
        )
    with pytest.raises(NotFoundError):
        await service.respond(
            conversation_id,
            "no-such-turn",
            TENANT,
            USER,
            ConversationApprovalDecision(decision="approved"),
        )
    # Still pending after the refused answers.
    assert (await settled(service, conversation_id)).status == MessageStatus.AWAITING_APPROVAL


async def test_a_turn_cannot_be_answered_twice() -> None:
    service, _, _, seen, conversation_id, paused = await paused_for_approval(
        tool_call_response("api.write", {"value": "draft"}), "saved"
    )
    assert paused.pending is not None
    approve = ConversationApprovalDecision(decision="approved")
    await service.respond(conversation_id, paused.pending.turn_id, TENANT, USER, approve)
    with pytest.raises(ConflictError):
        await service.respond(conversation_id, paused.pending.turn_id, TENANT, USER, approve)
    await settled(service, conversation_id, MessageStatus.COMPLETED)
    with pytest.raises(ConflictError, match="not waiting"):
        await service.respond(conversation_id, paused.pending.turn_id, TENANT, USER, approve)
    assert len(seen.calls) == 1


# --- clarification --------------------------------------------------------------------------------


async def test_turn_pauses_for_input_and_resumes_with_the_answer() -> None:
    seen = Seen()
    provider = MockModelProvider(["Account main is up to date."])
    service, _, _ = build(provider, seen, planning_strategy="ask_first")
    conversation_id = await start(service)
    await service.submit(conversation_id, TENANT, USER, "is my account up to date?")
    paused = await settled(service, conversation_id)

    assert paused.status == MessageStatus.AWAITING_INPUT
    assert paused.pending is not None
    assert paused.pending.kind == "input"
    assert paused.pending.question == "Which account?"
    assert paused.text_content == "Which account?"
    awaiting = next(
        e
        for e in await events(service, conversation_id)
        if e.type == "assistant.message.awaiting_input"
    )
    assert awaiting.data["pending"]["question"] == "Which account?"
    with pytest.raises(ConflictError, match="awaits an answer"):
        await service.respond(
            conversation_id,
            paused.pending.turn_id,
            TENANT,
            USER,
            ConversationApprovalDecision(decision="approved"),
        )

    await service.respond(
        conversation_id,
        paused.pending.turn_id,
        TENANT,
        USER,
        ConversationInputAnswer(text="the main one"),
    )
    done = await settled(service, conversation_id, MessageStatus.COMPLETED, MessageStatus.FAILED)

    assert done.id == paused.id
    assert done.status == MessageStatus.COMPLETED
    assert done.text_content == "Account main is up to date."
    assert any(message.text_content == "the main one" for message in provider.requests[-1].messages)


# --- expiry and cancellation -----------------------------------------------------------------------


async def test_answering_after_the_approval_expired_ends_the_turn_as_expired() -> None:
    service, _, stores, seen, conversation_id, paused = await paused_for_approval(
        tool_call_response("api.write", {"value": "draft"}), "saved"
    )
    assert paused.pending is not None and paused.pending.approval_id
    stores.approvals._items[paused.pending.approval_id].expires_at = utc_now() - timedelta(
        seconds=1
    )

    with pytest.raises(PendingTurnExpiredError):
        await service.respond(
            conversation_id,
            paused.pending.turn_id,
            TENANT,
            USER,
            ConversationApprovalDecision(decision="approved"),
        )

    done = await settled(service, conversation_id)
    assert done.status == MessageStatus.EXPIRED
    assert done.pending is None
    assert "assistant.message.expired" in types(await events(service, conversation_id))
    assert seen.calls == []
    # The conversation takes new messages again.
    await service.submit(conversation_id, TENANT, USER, "next")


async def test_maintenance_sweep_ends_turns_whose_approval_expired() -> None:
    service, runtime, stores, _, conversation_id, paused = await paused_for_approval(
        tool_call_response("api.write", {"value": "draft"}), "saved"
    )
    assert paused.pending is not None and paused.pending.approval_id
    stores.approvals._items[paused.pending.approval_id].expires_at = utc_now() - timedelta(
        seconds=1
    )
    maintenance = MaintenanceService(
        runtime, InMemoryArtifactStore(), MaintenanceSettings(), conversations=service
    )

    report = await maintenance.run_once()

    assert report.expired_paused_runs == 1
    assert report.reconciled_pending_turns == 1
    done = await settled(service, conversation_id)
    assert done.status == MessageStatus.EXPIRED
    assert done.metadata["reason"] == "approval_expired"


async def test_clarification_timeout_expires_the_turn() -> None:
    service, runtime, _ = build(MockModelProvider(["x"]), Seen(), planning_strategy="ask_first")
    conversation_id = await start(service)
    await service.submit(conversation_id, TENANT, USER, "hi")
    assert (await settled(service, conversation_id)).status == MessageStatus.AWAITING_INPUT

    assert await runtime.expire_paused_runs(clarification_timeout_seconds=0) == 1
    assert await service.reconcile_pending_turns() == 1

    done = await settled(service, conversation_id)
    assert done.status == MessageStatus.EXPIRED
    assert done.metadata["reason"] == "clarification_expired"


async def test_cancelling_a_pending_turn_ends_its_run() -> None:
    service, runtime, _, _, conversation_id, paused = await paused_for_approval(
        tool_call_response("api.write", {"value": "draft"}), "saved"
    )
    assert paused.pending is not None

    await service.cancel(conversation_id, TENANT)

    done = await settled(service, conversation_id)
    assert done.status == MessageStatus.CANCELLED
    assert done.pending is None
    assert types(await events(service, conversation_id))[-1] == "assistant.message.cancelled"
    assert (await runtime.status(paused.run_ids[0], TENANT)).status == RunStatus.CANCELLED
    with pytest.raises(ConflictError):
        await service.respond(
            conversation_id,
            paused.pending.turn_id,
            TENANT,
            USER,
            ConversationApprovalDecision(decision="approved"),
        )
    await service.submit(conversation_id, TENANT, USER, "something else")
    assert (await settled(service, conversation_id)).status == MessageStatus.COMPLETED


async def test_cancelling_a_resumed_turn_cancels_its_run() -> None:
    gate = asyncio.Event()

    class Slow(MockModelProvider):
        async def generate(self, request: Any) -> Any:
            if len(self.requests) >= 1:
                await gate.wait()
            return await super().generate(request)

    seen = Seen()
    service, runtime, _ = build(
        Slow([tool_call_response("api.write", {"value": "draft"}), "saved"]), seen
    )
    conversation_id = await start(service)
    await service.submit(conversation_id, TENANT, USER, "update it")
    paused = await settled(service, conversation_id)
    assert paused.pending is not None
    await service.respond(
        conversation_id,
        paused.pending.turn_id,
        TENANT,
        USER,
        ConversationApprovalDecision(decision="approved"),
    )
    for _ in range(200):
        if seen.calls:
            break
        await asyncio.sleep(0.005)

    await service.cancel(conversation_id, TENANT)
    done = await settled(service, conversation_id, MessageStatus.CANCELLED)

    assert done.id == paused.id
    for _ in range(200):
        if (await runtime.status(paused.run_ids[0], TENANT)).status == RunStatus.CANCELLED:
            break
        await asyncio.sleep(0.005)
    assert (await runtime.status(paused.run_ids[0], TENANT)).status == RunStatus.CANCELLED


async def test_a_new_message_while_a_turn_is_pending_is_a_conflict() -> None:
    service, _, _, _, conversation_id, paused = await paused_for_approval(
        tool_call_response("api.write", {"value": "draft"}), "saved"
    )
    assert paused.pending is not None

    with pytest.raises(ConflictError) as raised:
        await service.submit(conversation_id, TENANT, USER, "another question")

    assert "waiting for approval" in str(raised.value)
    assert paused.pending.turn_id in str(raised.value)
    assert len(await service.messages(conversation_id, TENANT)) == 2


# --- recovery --------------------------------------------------------------------------------------


async def test_pending_turn_survives_a_restart_and_resumes_once() -> None:
    seen = Seen()
    provider = MockModelProvider([tool_call_response("api.write", {"value": "draft"}), "saved"])
    service, runtime, stores = build(provider, seen)
    conversation_id = await start(service)
    await service.submit(conversation_id, TENANT, USER, "update it")
    paused = await settled(service, conversation_id)
    assert paused.pending is not None
    await service.shutdown(0.1)
    await runtime.shutdown(0.1)

    # A new process: new runtime and service objects over the same stores.
    restarted, restarted_runtime, _ = build(provider, seen, stores)
    assert await restarted_runtime.recover() == 0
    assert await restarted.recover() == 0
    assert (await settled(restarted, conversation_id)).status == MessageStatus.AWAITING_APPROVAL
    with pytest.raises(ConflictError, match="waiting for approval"):
        await restarted.submit(conversation_id, TENANT, USER, "again")

    await restarted.respond(
        conversation_id,
        paused.pending.turn_id,
        TENANT,
        USER,
        ConversationApprovalDecision(decision="approved"),
    )
    done = await settled(restarted, conversation_id, MessageStatus.COMPLETED, MessageStatus.FAILED)

    assert done.id == paused.id
    assert done.status == MessageStatus.COMPLETED
    assert done.text_content == "saved"
    assert len(seen.calls) == 1
    assert await restarted.recover() == 0
    assert len(seen.calls) == 1


async def test_restart_between_resume_and_message_update_follows_the_run_not_reruns_it() -> None:
    seen = Seen()
    provider = MockModelProvider([tool_call_response("api.write", {"value": "draft"}), "saved"])
    service, runtime, stores = build(provider, seen)
    conversation_id = await start(service)
    await service.submit(conversation_id, TENANT, USER, "update it")
    paused = await settled(service, conversation_id)
    assert paused.pending is not None and paused.pending.run_id
    # The run was resumed, but the process stopped before the message left awaiting_approval.
    await runtime.resume(paused.pending.run_id, TENANT, {"decision": "approved"})
    await runtime.wait(paused.pending.run_id, TENANT)

    restarted, _, _ = build(provider, seen, stores)
    assert await restarted.recover() == 1
    done = await settled(restarted, conversation_id, MessageStatus.COMPLETED, MessageStatus.FAILED)

    assert done.id == paused.id
    assert done.status == MessageStatus.COMPLETED
    assert done.text_content == "saved"
    assert len(seen.calls) == 1


# --- credentials -----------------------------------------------------------------------------------


async def test_credentials_reach_tools_on_submit_and_resume_and_never_leak(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    service, runtime, stores, seen, conversation_id, paused = await paused_for_approval(
        tool_call_response("api.read", {"value": "x"}),
        tool_call_response("api.write", {"value": "draft"}),
        "saved",
        credentials={"authorization": SECRET},
    )
    assert paused.pending is not None
    assert seen.calls == [("api.read", {"value": "x"}, {"authorization": SECRET})]

    await service.respond(
        conversation_id,
        paused.pending.turn_id,
        TENANT,
        USER,
        ConversationApprovalDecision(decision="approved"),
        credentials={"authorization": NEWER},
    )
    done = await settled(service, conversation_id, MessageStatus.COMPLETED, MessageStatus.FAILED)
    assert done.status == MessageStatus.COMPLETED
    # The resumed turn sees only the credentials passed to respond.
    assert seen.calls[-1] == ("api.write", {"value": "draft"}, {"authorization": NEWER})

    surfaces: list[str] = []
    surfaces.extend(m.model_dump_json() for m in await service.messages(conversation_id, TENANT))
    surfaces.extend(e.model_dump_json() for e in await events(service, conversation_id))
    surfaces.append((await service.get(conversation_id, TENANT)).model_dump_json())
    surfaces.extend(a.model_dump_json() for a in await stores.audits.list(TENANT))
    for run_id in done.run_ids:
        surfaces.append((await runtime.status(run_id, TENANT)).model_dump_json())
        surfaces.extend(e.model_dump_json() for e in await runtime.events.history(run_id))
    approval = await stores.approvals.get(str(paused.pending.approval_id), TENANT)
    assert approval is not None
    surfaces.append(approval.model_dump_json())
    captured = capsys.readouterr()
    surfaces.extend((caplog.text, captured.out, captured.err))
    for text in surfaces:
        assert SECRET not in text
        assert NEWER not in text
    assert runtime._credentials == {}


async def test_credentials_for_a_handler_that_cannot_take_them_are_refused() -> None:
    class Plain:
        name = "plain"

        async def handle(self, conversation: Any, user_message: Any, history: Any, emit: Any):  # type: ignore[no-untyped-def]
            raise AssertionError("not called")

    handlers = ConversationHandlerRegistry()
    handlers.register(Plain())
    service = ConversationService(
        InMemoryConversationStore(), InMemoryConversationEventBus(), handlers
    )
    conversation = await service.create(
        tenant_id=TENANT, user_id=USER, agent="test-agent", handler="plain"
    )

    with pytest.raises(ConfigurationError, match="does not accept credentials"):
        await service.submit(
            conversation.id, TENANT, USER, "hi", credentials={"authorization": SECRET}
        )
    with pytest.raises(TypeError):
        await service.submit(conversation.id, TENANT, USER, "hi", credentials={"a": 1})  # type: ignore[dict-item]
    assert await service.messages(conversation.id, TENANT) == ()


# --- isolation -------------------------------------------------------------------------------------


async def test_respond_is_tenant_and_owner_isolated() -> None:
    service, _, _, seen, conversation_id, paused = await paused_for_approval(
        tool_call_response("api.write", {"value": "draft"}), "saved"
    )
    assert paused.pending is not None
    approve = ConversationApprovalDecision(decision="approved")

    with pytest.raises(NotFoundError):
        await service.respond(conversation_id, paused.pending.turn_id, "tenant-b", USER, approve)
    with pytest.raises(NotFoundError):
        await service.respond(conversation_id, paused.pending.turn_id, TENANT, "user-b", approve)
    assert (await settled(service, conversation_id)).status == MessageStatus.AWAITING_APPROVAL
    assert seen.calls == []


# --- configuration ---------------------------------------------------------------------------------


def test_container_wires_streaming_settings_and_the_maintenance_hook() -> None:
    from algen_agent_runtime.config.settings import AppSettings
    from algen_agent_runtime.orchestration.container import build_container

    container = build_container(
        AppSettings.model_validate(
            {"conversation_streaming": {"enabled": False, "delta_interval_ms": 200}}
        )
    )
    handler: Any = container.conversations.handlers.get("runtime")
    assert handler._stream is False
    assert handler._delta_interval == 0.2
    assert container.maintenance is not None
    assert container.maintenance._conversations is container.conversations


# --- recovery of streaming turns -----------------------------------------------------------------

HANG = "<hang>"


class HangingProvider(MockModelProvider):
    """Answers as scripted, except that a ``HANG`` reply blocks until the test ends: the process
    'dies' mid-turn, with the model working."""

    def __init__(self, responses: list[Any]) -> None:
        super().__init__(responses)
        self.hanging = asyncio.Event()

    async def generate(self, request: Any) -> Any:
        if self._responses and self._responses[0] == HANG:
            self.hanging.set()
            await asyncio.Event().wait()
        return await super().generate(request)


async def hung_turn(
    *responses: Any, handler_options: dict[str, Any] | None = None
) -> tuple[ConversationService, AgentRuntime, Stores, Seen, str, ConversationMessage]:
    """A turn whose model hangs after ``responses``; the first process is then abandoned."""
    seen = Seen()
    provider = HangingProvider([*responses, HANG])
    service, runtime, stores = build(provider, seen, handler_options=handler_options)
    conversation_id = await start(service)
    _, assistant = await service.submit(conversation_id, TENANT, USER, "which roles?")
    await asyncio.wait_for(provider.hanging.wait(), 5)
    return service, runtime, stores, seen, conversation_id, assistant


async def rewind_to_streaming(
    stores: Stores, message: ConversationMessage, **update: Any
) -> ConversationMessage:
    """The message as a process that stopped before storing the turn's end left it."""
    rewound = message.model_copy(
        update={"status": MessageStatus.STREAMING, "content": (), "pending": None, **update}
    )
    await stores.conversations.update_message(rewound)
    return rewound


async def test_the_run_id_is_stored_on_the_message_while_the_turn_streams() -> None:
    _, runtime, stores, _, conversation_id, assistant = await hung_turn()
    message = await stores.conversations.messages(conversation_id, TENANT, limit=10)
    streaming = next(m for m in message if m.id == assistant.id)
    assert streaming.status == MessageStatus.STREAMING
    (run_id,) = streaming.run_ids
    assert (await runtime.status(run_id, TENANT)).request.turn_id == streaming.reply_to_message_id
    started = [
        e
        for e in stores.conversation_events._history[conversation_id]
        if e.type == "agent.run.started"
    ]
    assert [e.data["run_id"] for e in started] == [run_id]


async def test_a_restart_mid_turn_follows_the_recovered_run_to_its_answer() -> None:
    _, _, stores, seen, conversation_id, assistant = await hung_turn(
        tool_call_response("api.read", {"value": "roles"})
    )
    # A new process on the same stores: the runtime recovers the run, then the conversations.
    restarted, restarted_runtime, _ = build(
        MockModelProvider(["Here are the roles."]), seen, stores
    )
    assert await restarted_runtime.recover() == 1
    assert await restarted.recover() == 1
    done = await settled(restarted, conversation_id)

    assert done.id == assistant.id
    assert done.status == MessageStatus.COMPLETED, done.content
    assert done.text_content == "Here are the roles."
    assert len(done.run_ids) == 1
    assert [call[0] for call in seen.calls] == ["api.read"]  # not run again
    history = types(await events(restarted, conversation_id))
    assert "assistant.message.failed" not in history
    assert history.count("assistant.message.completed") == 1
    assert await restarted.recover() == 0  # settled once


async def test_a_restart_before_the_run_id_was_stored_finds_the_turns_run() -> None:
    _, _, stores, seen, conversation_id, assistant = await hung_turn()
    message = await stored_message(stores, conversation_id, assistant.id)
    await stores.conversations.update_message(message.model_copy(update={"run_ids": ()}))

    restarted, restarted_runtime, _ = build(MockModelProvider(["found it"]), seen, stores)
    await restarted_runtime.recover()
    assert await restarted.recover() == 1
    done = await settled(restarted, conversation_id)

    assert done.status == MessageStatus.COMPLETED
    assert done.text_content == "found it"
    assert done.run_ids == message.run_ids


async def stored_message(stores: Stores, conversation_id: str, message_id: str) -> Any:
    items = await stores.conversations.messages(conversation_id, TENANT, limit=100)
    return next(m for m in items if m.id == message_id)


async def test_a_run_that_completed_while_down_settles_the_turn_with_its_answer() -> None:
    seen = Seen()
    service, _, stores = build(MockModelProvider(["all done"]), seen)
    conversation_id = await start(service)
    await service.submit(conversation_id, TENANT, USER, "go")
    done = await settled(service, conversation_id)
    await rewind_to_streaming(stores, done)

    restarted, _, _ = build(MockModelProvider([]), seen, stores)
    assert await restarted.recover() == 1
    again = await settled(restarted, conversation_id)
    assert again.status == MessageStatus.COMPLETED
    assert again.text_content == "all done"


async def test_a_run_that_paused_while_down_moves_the_turn_to_awaiting_approval() -> None:
    _, _, stores, seen, conversation_id, paused = await paused_for_approval(
        tool_call_response("api.write", {"value": "draft"}), "saved"
    )
    await rewind_to_streaming(stores, paused)

    restarted, _, _ = build(MockModelProvider(["saved"]), seen, stores)
    assert await restarted.recover() == 1
    waiting = await settled(restarted, conversation_id)
    assert waiting.status == MessageStatus.AWAITING_APPROVAL
    assert waiting.pending is not None and paused.pending is not None
    assert waiting.pending.approval_id == paused.pending.approval_id
    assert waiting.pending.redacted_parameters == {"value": "draft"}
    await restarted.respond(
        conversation_id,
        waiting.pending.turn_id,
        TENANT,
        USER,
        ConversationApprovalDecision(decision="approved"),
    )
    done = await settled(restarted, conversation_id, MessageStatus.COMPLETED, MessageStatus.FAILED)
    assert done.text_content == "saved"
    assert len(seen.calls) == 1


async def test_a_run_whose_approval_expired_while_down_ends_the_turn_as_expired() -> None:
    _, _, stores, seen, conversation_id, paused = await paused_for_approval(
        tool_call_response("api.write", {"value": "draft"})
    )
    assert paused.pending is not None and paused.pending.approval_id
    approvals = stores.approvals._items
    approval = approvals[paused.pending.approval_id]
    approvals[approval.id] = approval.model_copy(
        update={"expires_at": utc_now() - timedelta(seconds=1)}
    )
    await rewind_to_streaming(stores, paused)

    restarted, _, _ = build(MockModelProvider([]), seen, stores)
    assert await restarted.recover() == 1
    expired = await settled(restarted, conversation_id)
    assert expired.status == MessageStatus.EXPIRED
    assert expired.pending is None
    assert seen.calls == []


async def test_a_run_that_failed_while_down_fails_the_turn_with_its_own_outcome() -> None:
    seen = Seen()
    service, runtime, stores = build(MockModelProvider(["fine"]), seen)
    conversation_id = await start(service)
    await service.submit(conversation_id, TENANT, USER, "go")
    done = await settled(service, conversation_id)
    state = await runtime.status(done.run_ids[0], TENANT)
    expected = state.version
    state.status = RunStatus.FAILED
    state.error = "Verification failed."
    state.output_text = None
    await stores.runs.save(state, expected)
    await rewind_to_streaming(stores, done)

    restarted, _, _ = build(MockModelProvider([]), seen, stores)
    assert await restarted.recover() == 1
    failed = await settled(restarted, conversation_id)
    assert failed.status == MessageStatus.FAILED
    assert failed.metadata.get("error_type") != "InterruptedResponse"
    assert failed.metadata["retryable"] is False
    assert failed.metadata["run_status"] == "failed"
    assert "restart" not in failed.text_content


async def test_a_cancelled_run_cancels_the_turn() -> None:
    _, runtime, stores, seen, conversation_id, assistant = await hung_turn()
    message = await stored_message(stores, conversation_id, assistant.id)
    await runtime.cancel(message.run_ids[0], TENANT)

    restarted, _, _ = build(MockModelProvider([]), seen, stores)
    assert await restarted.recover() == 1
    cancelled = await settled(restarted, conversation_id)
    assert cancelled.status == MessageStatus.CANCELLED


async def test_a_turn_whose_run_is_gone_or_never_started_fails_as_interrupted() -> None:
    seen = Seen()
    service, _, stores = build(MockModelProvider([]), seen)
    gone_id = await start(service)
    never_id = await start(service)
    for conversation_id, run_ids in ((gone_id, ("run-that-is-gone",)), (never_id, ())):
        user = ConversationMessage.text(
            conversation_id=conversation_id, tenant_id=TENANT, role="user", text="hello"
        )
        await stores.conversations.append_message(user)
        await stores.conversations.append_message(
            ConversationMessage(
                conversation_id=conversation_id,
                tenant_id=TENANT,
                role="assistant",
                content=(),
                status=MessageStatus.STREAMING,
                reply_to_message_id=user.id,
                run_ids=run_ids,
            )
        )

    assert await service.recover() == 2
    for conversation_id in (gone_id, never_id):
        failed = await settled(service, conversation_id)
        assert failed.status == MessageStatus.FAILED
        assert failed.metadata == {"error_type": "InterruptedResponse", "retryable": True}
        assert "Please retry" in failed.text_content
    assert await service.recover() == 0


async def test_a_run_that_nothing_drives_past_its_deadline_fails_as_interrupted() -> None:
    _, runtime, stores, seen, conversation_id, assistant = await hung_turn()
    message = await stored_message(stores, conversation_id, assistant.id)
    state = await runtime.status(message.run_ids[0], TENANT)
    expected = state.version
    state.deadline = utc_now() - timedelta(minutes=5)
    await stores.runs.save(state, expected)

    # No Runtime.recover: nothing drives the run in the new process.
    restarted, _, _ = build(MockModelProvider([]), seen, stores)
    assert await restarted.recover() == 1
    failed = await settled(restarted, conversation_id)
    assert failed.metadata["error_type"] == "InterruptedResponse"


async def test_two_processes_recovering_at_once_settle_the_turn_once() -> None:
    seen = Seen()
    service, _, stores = build(MockModelProvider(["once"]), seen)
    conversation_id = await start(service)
    await service.submit(conversation_id, TENANT, USER, "go")
    done = await settled(service, conversation_id)
    before = len(await events(service, conversation_id))
    await rewind_to_streaming(stores, done)

    first, _, _ = build(MockModelProvider([]), seen, stores)
    second, _, _ = build(MockModelProvider([]), seen, stores)
    await asyncio.gather(first.recover(), second.recover())
    again = await settled(first, conversation_id)
    await asyncio.sleep(0.05)

    assert again.status == MessageStatus.COMPLETED and again.text_content == "once"
    added = types((await events(first, conversation_id))[before:])
    assert added.count("assistant.message.completed") == 1
    assert "assistant.message.failed" not in added


async def test_follow_reattaches_to_a_streaming_turn_and_is_idempotent() -> None:
    _, _, stores, seen, conversation_id, assistant = await hung_turn()
    restarted, restarted_runtime, _ = build(MockModelProvider(["followed"]), seen, stores)
    await restarted_runtime.recover()  # the host recovers runs but not conversations
    turn_id = assistant.reply_to_message_id
    assert turn_id is not None

    with pytest.raises(NotFoundError):
        await restarted.follow(conversation_id, turn_id, TENANT, "another-user")
    following = await restarted.follow(conversation_id, turn_id, TENANT, USER)
    assert following.status == MessageStatus.STREAMING
    again = await restarted.follow(conversation_id, turn_id, TENANT, USER)
    assert again.id == assistant.id
    done = await settled(restarted, conversation_id)
    assert done.status == MessageStatus.COMPLETED and done.text_content == "followed"
    settled_again = await restarted.follow(conversation_id, turn_id, TENANT, USER)
    assert settled_again.status == MessageStatus.COMPLETED
    assert types(await events(restarted, conversation_id)).count("assistant.message.completed") == 1


async def test_follow_leaves_a_turn_without_a_run_as_it_is() -> None:
    seen = Seen()
    service, _, stores = build(MockModelProvider([]), seen)
    conversation_id = await start(service)
    user = ConversationMessage.text(
        conversation_id=conversation_id, tenant_id=TENANT, role="user", text="hello"
    )
    await stores.conversations.append_message(user)
    await stores.conversations.append_message(
        ConversationMessage(
            conversation_id=conversation_id,
            tenant_id=TENANT,
            role="assistant",
            content=(),
            status=MessageStatus.ACCEPTED,
            reply_to_message_id=user.id,
        )
    )
    message = await service.follow(conversation_id, user.id, TENANT, USER)
    assert message.status == MessageStatus.ACCEPTED


# --- tool steps, run input, owner-checked messages ---------------------------------------------


async def test_tool_steps_reach_the_conversation_without_arguments_or_results() -> None:
    service, _, _ = build(
        MockModelProvider([tool_call_response("api.read", {"value": "secret-arg"}), "done"]),
        Seen(),
        handler_options={"tool_steps": True, "delta_interval_seconds": 0},
    )
    conversation_id = await start(service)
    await service.submit(conversation_id, TENANT, USER, "read it")
    await settled(service, conversation_id)
    history = await events(service, conversation_id)
    started = [e for e in history if e.type == "agent.tool.started"]
    completed = [e for e in history if e.type == "agent.tool.completed"]
    assert [e.data["tool"] for e in started] == ["api.read"]
    assert started[0].data["argument_keys"] == ["value"]
    assert completed[0].data["status"] == "completed"
    assert completed[0].data["call_id"] == started[0].data["call_id"]
    assert "result_chars" in completed[0].data
    assert "secret-arg" not in str([e.data for e in started + completed])
    assert types(history).index("agent.tool.completed") < types(history).index(
        "assistant.message.completed"
    )


async def test_tool_steps_are_off_by_default() -> None:
    service, _, _ = build(
        MockModelProvider([tool_call_response("api.read", {"value": "x"}), "done"]), Seen()
    )
    conversation_id = await start(service)
    await service.submit(conversation_id, TENANT, USER, "read it")
    await settled(service, conversation_id)
    assert not {"agent.tool.started", "agent.tool.completed"} & set(
        types(await events(service, conversation_id))
    )


async def test_run_input_shapes_the_runs_input_and_keeps_the_message_text() -> None:
    service, runtime, _ = build(MockModelProvider(["ok"]), Seen())
    conversation_id = await start(service)
    user, _ = await service.submit(
        conversation_id,
        TENANT,
        USER,
        "what changed?",
        run_input="Today is 2026-11-11.\nwhat changed?",
    )
    done = await settled(service, conversation_id)
    run = await runtime.status(done.run_ids[0], TENANT)
    assert run.request.input == "Today is 2026-11-11.\nwhat changed?"
    assert user.text_content == "what changed?"
    assert (await service.messages(conversation_id, TENANT))[0].text_content == "what changed?"
    with pytest.raises(ValueError):
        await service.submit(conversation_id, TENANT, USER, "again", run_input="  ")


async def test_run_input_needs_a_handler_that_accepts_it() -> None:
    class Plain:
        name = "plain"

        async def handle(
            self, conversation: Any, user_message: Any, history: Any, emit: Any
        ) -> Any:
            raise AssertionError("not called")

    handlers = ConversationHandlerRegistry()
    handlers.register(Plain())
    service = ConversationService(
        InMemoryConversationStore(), InMemoryConversationEventBus(), handlers
    )
    conversation = await service.create(tenant_id=TENANT, user_id=USER, agent="a", handler="plain")
    with pytest.raises(ConfigurationError, match="run_input"):
        await service.submit(conversation.id, TENANT, USER, "hi", run_input="shaped")


async def test_messages_with_user_id_apply_the_owner_check() -> None:
    service, _, _ = build(MockModelProvider(["ok"]), Seen())
    conversation_id = await start(service)
    await service.submit(conversation_id, TENANT, USER, "hi")
    await settled(service, conversation_id)
    assert len(await service.messages(conversation_id, TENANT, user_id=USER)) == 2
    with pytest.raises(NotFoundError):
        await service.messages(conversation_id, TENANT, user_id="someone-else")
    assert len(await service.messages(conversation_id, TENANT)) == 2  # unchanged default


async def test_conditional_message_update_only_replaces_an_expected_status() -> None:
    store = InMemoryConversationStore()
    message = ConversationMessage.text(
        conversation_id="c", tenant_id=TENANT, role="assistant", text="", status="streaming"
    )
    await store.append_message(message)
    done = message.model_copy(update={"status": MessageStatus.COMPLETED})
    assert await store.update_message_if(done, {MessageStatus.STREAMING})
    assert not await store.update_message_if(done, {MessageStatus.STREAMING})
    gone = done.model_copy(update={"id": "missing"})
    assert not await store.update_message_if(gone, {MessageStatus.COMPLETED})


async def test_the_memory_run_store_finds_a_turns_run() -> None:
    _, _, stores, _, conversation_id, assistant = await hung_turn()
    message = await stored_message(stores, conversation_id, assistant.id)
    assert message.reply_to_message_id is not None
    found = await stores.runs.find_by_turn(TENANT, conversation_id, message.reply_to_message_id)
    assert found is not None and found.id == message.run_ids[0]
    assert (
        await stores.runs.find_by_turn("other-tenant", conversation_id, message.reply_to_message_id)
        is None
    )
    assert await stores.runs.find_by_turn(TENANT, conversation_id, "other-turn") is None


def test_migration_022_only_adds_an_index() -> None:
    from pathlib import Path

    migration = (
        Path(__file__).parents[2]
        / "src/algen_agent_runtime/persistence/migrations/022_run_turn_index.sql"
    ).read_text(encoding="utf-8")
    statements = [
        line for line in migration.splitlines() if line.strip() and not line.startswith("--")
    ]
    assert statements[0].startswith("CREATE INDEX IF NOT EXISTS algen_agent_runtime_runs_turn_idx")
    for word in ("DROP", "ALTER", "DELETE", "UPDATE"):
        assert word not in migration.upper().replace("UPDATED", "")


# --- shutdown detaches, failure reasons, events, steering, agent per turn, HTTP ----------------


async def test_shutdown_detaches_turns_so_the_next_start_completes_them() -> None:
    service, runtime, stores, seen, conversation_id, assistant = await hung_turn()
    await service.shutdown(0.05)
    await runtime.shutdown(0.05)

    message = await stored_message(stores, conversation_id, assistant.id)
    assert message.status == MessageStatus.STREAMING  # not "Response cancelled."
    run = await stores.runs.get(message.run_ids[0], TENANT)
    assert run is not None and run.status not in {RunStatus.CANCELLED, RunStatus.FAILED}
    assert "assistant.message.cancelled" not in types(await events(service, conversation_id))

    restarted, restarted_runtime, _ = build(MockModelProvider(["after the deploy"]), seen, stores)
    await restarted_runtime.recover()
    await restarted.recover()
    done = await settled(restarted, conversation_id)
    assert done.status == MessageStatus.COMPLETED and done.text_content == "after the deploy"


async def test_a_users_cancel_still_cancels_the_turn_and_its_run() -> None:
    service, runtime, _, _, conversation_id, _ = await hung_turn()
    await service.cancel(conversation_id, TENANT)
    cancelled = await settled(service, conversation_id)
    assert cancelled.status == MessageStatus.CANCELLED
    run = await runtime.status(cancelled.run_ids[0], TENANT)
    assert run.status == RunStatus.CANCELLED


class RefusingMeter:
    async def reserve(self, *, max_input_tokens: int, max_output_tokens: int, call: Any) -> object:
        raise MeterRefusedError("tenant acct-42 has 0 credits left")

    async def settle(self, ticket: object, usage: Any) -> None:
        return None

    async def release(self, ticket: object) -> None:
        return None


async def test_a_refused_run_fails_the_turn_with_its_reason_and_a_user_safe_text() -> None:
    service, _, _ = build(MockModelProvider(["never"]), Seen(), usage_meter=RefusingMeter())
    conversation_id = await start(service)
    await service.submit(conversation_id, TENANT, USER, "hello")
    failed = await settled(service, conversation_id)

    assert failed.status == MessageStatus.FAILED
    assert failed.text_content == RUN_FAILURE_MESSAGES["budget_refused"]
    assert "acct-42" not in failed.text_content
    assert failed.metadata["error_type"] == "RunFailed"
    assert failed.metadata["reason"] == "budget_refused"
    assert failed.metadata["run_status"] == "failed"
    assert failed.metadata["retryable"] is False
    assert failed.metadata["usage"]["input_tokens"] == 0  # refused before any call
    # The run's own typed error rides along (RunResult.error_type); error_type stays RunFailed.
    assert failed.metadata["run_error_type"] == "MeterRefusedError"
    assert "limit_reached" not in failed.metadata
    assert failed.run_ids


def test_failure_texts_are_a_fixed_mapping_a_host_can_replace() -> None:
    presentation = ConversationPresentation(failure_messages={"budget_refused": "Out of credits."})
    assert presentation.run_failure_message("budget_refused", "raw detail") == "Out of credits."
    assert "raw detail" not in presentation.run_failure_message("other", "raw detail")
    developer = ConversationPresentation(error_audience=PresentationAudience.DEVELOPER)
    assert developer.run_failure_message("other", "raw detail") == "raw detail"


async def test_concurrent_events_on_one_conversation_get_distinct_sequences() -> None:
    service, _, _ = build(MockModelProvider([]), Seen())
    conversation = await service.create(tenant_id=TENANT, user_id=USER, agent="test-agent")
    await asyncio.gather(*(service._emit(conversation, "test.event", {"n": n}) for n in range(50)))
    sequences = [event.sequence for event in await service.events.history(conversation.id)]
    assert sequences == list(range(1, len(sequences) + 1)) and len(sequences) == 51
    assert len(service._emit_locks) == 0 and len(service._locks) == 0  # dropped once released


class FlakyBus(InMemoryConversationEventBus):
    """Fails to publish progress and delta events, as a store under contention might."""

    async def publish(self, event: Any) -> None:
        if event.type in {"assistant.message.delta", "agent.status.changed", "workflow.progress"}:
            raise ConflictError("conversation event sequence is taken")
        await super().publish(event)


async def test_a_lost_progress_event_does_not_abandon_a_running_turn() -> None:
    stores = Stores(conversation_events=FlakyBus())
    service, _, _ = build(
        MockModelProvider(["the answer survives"]),
        Seen(),
        stores,
        handler_options={"delta_interval_seconds": 0},
    )
    conversation_id = await start(service)
    await service.submit(conversation_id, TENANT, USER, "go")
    done = await settled(service, conversation_id)
    assert done.status == MessageStatus.COMPLETED
    assert done.text_content == "the answer survives"


class GatedProvider(MockModelProvider):
    """Holds its first call until ``gate`` is set."""

    def __init__(self, responses: list[Any]) -> None:
        super().__init__(responses)
        self.gate = asyncio.Event()
        self.waiting = asyncio.Event()

    async def generate(self, request: Any) -> Any:
        if not self.gate.is_set():
            self.waiting.set()
            await self.gate.wait()
        return await super().generate(request)


async def test_steer_reaches_the_turns_run_and_is_reported_on_the_conversation() -> None:
    provider = GatedProvider([tool_call_response("api.read", {"value": "x"}), "steered answer"])
    service, _, _ = build(provider, Seen())
    conversation_id = await start(service)
    user, _ = await service.submit(conversation_id, TENANT, USER, "go")
    await asyncio.wait_for(provider.waiting.wait(), 5)

    with pytest.raises(NotFoundError):
        await service.steer(conversation_id, user.id, TENANT, "another-user", "focus on Pune")
    steering = await service.steer(conversation_id, user.id, TENANT, USER, "focus on Pune")
    provider.gate.set()
    done = await settled(service, conversation_id)

    assert done.status == MessageStatus.COMPLETED
    assert "focus on Pune" in provider.requests[-1].model_dump_json()
    history = await events(service, conversation_id)
    steered = next(e for e in history if e.type == "assistant.message.steered")
    assert steered.data == {"turn_id": user.id, "steer_id": steering.id, "chars": 13}
    delivered = next(e for e in history if e.type == "agent.steer.delivered")
    assert delivered.data["messages"] == [{"id": steering.id, "chars": 13}]
    assert "focus on Pune" not in str([e.data for e in history if e.type.startswith("agent.")])
    with pytest.raises(ConflictError):
        await service.steer(conversation_id, user.id, TENANT, USER, "too late")


async def test_a_turn_may_choose_an_allowed_agent() -> None:
    service, runtime, _ = build(
        MockModelProvider(["from the other agent", "from the default"]),
        Seen(),
        extra_agents=("other-agent",),
    )
    conversation = await service.create(
        tenant_id=TENANT, user_id=USER, agent="test-agent", allowed_agents=("other-agent",)
    )
    _, assistant = await service.submit(conversation.id, TENANT, USER, "hi", agent="other-agent")
    assert assistant.agent == "other-agent"
    done = await settled(service, conversation.id)
    assert (await runtime.status(done.run_ids[0], TENANT)).request.agent == "other-agent"
    assert done.agent == "other-agent"

    _, default = await service.submit(conversation.id, TENANT, USER, "again")
    assert default.agent is None
    done = await settled(service, conversation.id)
    assert (await runtime.status(done.run_ids[0], TENANT)).request.agent == "test-agent"
    with pytest.raises(ValueError, match="not allowed"):
        await service.submit(conversation.id, TENANT, USER, "x", agent="unlisted-agent")
    # Unset, the new fields are left out of what is stored, so earlier releases still read it.
    plain = await service.create(tenant_id=TENANT, user_id=USER, agent="test-agent")
    assert "allowed_agents" not in plain.model_dump()
    assert "agent" not in default.model_dump()


def _app(service: ConversationService, runtime: AgentRuntime, **api: Any) -> Any:
    from algen_agent_runtime.api.app import create_app
    from algen_agent_runtime.config.settings import ApiSettings, AppSettings
    from algen_agent_runtime.orchestration.container import Container
    from algen_agent_runtime.persistence.memory import InMemoryArtifactStore

    container = Container(
        runtime=runtime,
        agents=runtime.agents,
        tools=runtime.tools,
        artifacts=InMemoryArtifactStore(),
        events=runtime.events,
        conversations=service,
    )
    return create_app(AppSettings(api=ApiSettings(**api)), container)


HEADERS = {
    "x-tenant-id": TENANT,
    "x-user-id": USER,
    "x-scopes": "conversations:read conversations:write",
}


async def test_http_conversation_routes_forward_only_configured_credential_headers() -> None:
    import httpx

    for configured, expected in (((), {}), (("X-CRM-Token",), {"x-crm-token": SECRET})):
        seen = Seen()
        provider = MockModelProvider([tool_call_response("api.read", {"value": "x"}), "done"])
        service, runtime, _ = build(provider, seen)
        app = _app(service, runtime, credential_headers=configured)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/v1/conversations", headers=HEADERS, json={"agent": "test-agent"}
            )
            conversation_id = created.json()["id"]
            sent = await client.post(
                f"/v1/conversations/{conversation_id}/messages",
                headers={**HEADERS, "x-crm-token": SECRET, "x-other": "not forwarded"},
                json={"text": "read it"},
            )
            assert sent.status_code == 202, sent.text
            await settled(service, conversation_id)
            refused = await client.post(
                f"/v1/conversations/{conversation_id}/messages",
                headers=HEADERS,
                json={"text": "x", "agent": "unlisted-agent"},
            )
            assert refused.status_code == 422
            steer = await client.post(
                f"/v1/conversations/{conversation_id}/turns/no-such-turn/steer",
                headers=HEADERS,
                json={"text": "more"},
            )
            assert steer.status_code == 404
        assert [call[2] for call in seen.calls] == [expected]
        assert SECRET not in str(await events(service, conversation_id))


def test_credential_header_names_are_validated() -> None:
    from algen_agent_runtime.config.settings import ApiSettings

    assert ApiSettings(credential_headers=("Authorization ",)).credential_headers == (
        "authorization",
    )
    with pytest.raises(ValueError):
        ApiSettings(credential_headers=("bad header",))


# --- credentials pause after a restart (pauses x conversation recovery) ---------------------------


async def credentials_paused_turn() -> tuple[
    ConversationService, AgentRuntime, Stores, Seen, str, ConversationMessage
]:
    """A turn started with caller credentials whose process stopped mid-run; the restarted
    runtime recovers the run into ``awaiting_credentials`` and the conversation follows it."""
    seen = Seen()
    provider = HangingProvider([tool_call_response("api.read", {"value": "roles"}), HANG])
    service, _, stores = build(provider, seen)
    conversation_id = await start(service)
    await service.submit(
        conversation_id, TENANT, USER, "which roles?", credentials={"authorization": SECRET}
    )
    await asyncio.wait_for(provider.hanging.wait(), 5)

    restarted, runtime, _ = build(
        MockModelProvider([tool_call_response("api.read", {"value": "more"}), "Here they are."]),
        seen,
        stores,
    )
    # Paused for credentials, not driven again here, so not counted as recovered.
    assert await runtime.recover() == 0
    assert await restarted.recover() == 1
    waiting = await settled(restarted, conversation_id, MessageStatus.AWAITING_CREDENTIALS)
    return restarted, runtime, stores, seen, conversation_id, waiting


async def test_a_run_recovered_without_its_credentials_awaits_them_not_an_approval() -> None:
    service, runtime, _, seen, conversation_id, waiting = await credentials_paused_turn()

    assert waiting.pending is not None
    assert waiting.pending.kind == "credentials"
    assert waiting.pending.approval_id is None
    (run_id,) = waiting.run_ids
    assert waiting.pending.run_id == run_id
    assert (await runtime.status(run_id, TENANT)).status == RunStatus.AWAITING_CREDENTIALS
    history = types(await events(service, conversation_id))
    assert "assistant.message.awaiting_credentials" in history
    assert "assistant.message.awaiting_approval" not in history
    assert SECRET not in waiting.model_dump_json()

    # A new message is refused while the turn waits, naming what it waits for.
    with pytest.raises(ConflictError, match="waiting for credentials"):
        await service.submit(conversation_id, TENANT, USER, "hello?")
    # An approval decision, or a renewal without credentials, does not continue it.
    with pytest.raises(ConflictError, match="awaits the caller's credentials"):
        await service.respond(
            conversation_id,
            waiting.pending.turn_id,
            TENANT,
            USER,
            ConversationApprovalDecision(decision="approved"),
        )
    with pytest.raises(ConflictError, match="pass them again"):
        await service.respond(
            conversation_id, waiting.pending.turn_id, TENANT, USER, ConversationCredentialsRenewal()
        )
    assert (await runtime.status(run_id, TENANT)).status == RunStatus.AWAITING_CREDENTIALS

    # The user's next action passes them again: the run continues where it stopped.
    resumed = await service.respond(
        conversation_id,
        waiting.pending.turn_id,
        TENANT,
        USER,
        ConversationCredentialsRenewal(),
        credentials={"authorization": NEWER},
    )
    assert resumed.id == waiting.id and resumed.status == MessageStatus.STREAMING
    done = await settled(service, conversation_id, MessageStatus.COMPLETED, MessageStatus.FAILED)
    assert done.status == MessageStatus.COMPLETED, done.content
    assert done.text_content == "Here they are."
    assert [call[2] for call in seen.calls] == [
        {"authorization": SECRET},
        {"authorization": NEWER},
    ]
    assert NEWER not in str(await events(service, conversation_id))


async def test_http_credentials_route_continues_a_turn_with_the_configured_headers() -> None:
    import httpx

    service, runtime, _, seen, conversation_id, waiting = await credentials_paused_turn()
    assert waiting.pending is not None
    turn_id = waiting.pending.turn_id
    url = f"/v1/conversations/{conversation_id}/turns/{turn_id}/credentials"

    # No credential headers configured: nothing to continue with, so the route refuses.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service, runtime)), base_url="http://test"
    ) as client:
        refused = await client.post(url, headers={**HEADERS, "x-crm-token": NEWER})
        assert refused.status_code == 409, refused.text

    app = _app(service, runtime, credential_headers=("X-CRM-Token",))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        accepted = await client.post(url, headers={**HEADERS, "x-crm-token": NEWER})
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["status"] == "streaming"
    done = await settled(service, conversation_id, MessageStatus.COMPLETED, MessageStatus.FAILED)
    assert done.status == MessageStatus.COMPLETED
    assert seen.calls[-1][2] == {"x-crm-token": NEWER}

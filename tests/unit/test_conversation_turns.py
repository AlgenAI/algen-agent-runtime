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
    ConversationHandlerRegistry,
    ConversationInputAnswer,
    ConversationMessage,
    ConversationService,
    MessageStatus,
    PendingTurnExpiredError,
    RuntimeConversationHandler,
)
from algen_agent_runtime.conversations.stores import (
    InMemoryConversationEventBus,
    InMemoryConversationStore,
)
from algen_agent_runtime.conversations.streaming import DeltaCoalescer
from algen_agent_runtime.events.bus import InMemoryAuditLog, InMemoryEventBus
from algen_agent_runtime.exceptions.errors import ConfigurationError, ConflictError, NotFoundError
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
    **agent: Any,
) -> tuple[ConversationService, AgentRuntime, Stores]:
    """A runtime and a conversation service over ``stores``; call again to 'restart'."""
    stores = stores or Stores()
    agents = InMemoryAgentRegistry()
    agents.register(
        make_agent(enabled_tools=frozenset({"api.read", "api.write"}), max_steps=12, **agent)
    )
    tools = ToolRegistry()
    tools.register(seen.tool("api.read", SideEffect.READ))
    tools.register(seen.tool("api.write", SideEffect.WRITE))
    policies = CompositePolicyEngine()
    router = ModelRouter(circuit_failure_threshold=1)
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
    service, _, _, seen, conversation_id, paused = await paused_for_approval(
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

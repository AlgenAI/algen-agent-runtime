"""A paused conversation turn survives a restart on PostgreSQL stores and resumes once."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from conftest import make_agent

from algen_agent_runtime.approvals.service import PostgresApprovalService
from algen_agent_runtime.config.registry import InMemoryAgentRegistry
from algen_agent_runtime.context.builder import ContextBuilderRegistry, DefaultContextBuilder
from algen_agent_runtime.conversations import (
    ConversationApprovalDecision,
    ConversationHandlerRegistry,
    ConversationMessage,
    ConversationService,
    MessageStatus,
    RuntimeConversationHandler,
)
from algen_agent_runtime.conversations.stores import (
    PostgresConversationEventBus,
    PostgresConversationStore,
)
from algen_agent_runtime.events.bus import PostgresAuditLog, PostgresEventBus
from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.persistence.postgres import (
    PostgresDatabase,
    PostgresMemoryStore,
    PostgresRunStore,
)
from algen_agent_runtime.planning.planners import PlannerRegistry
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.responses.composer import ResponseComposerRegistry
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.verification.verifiers import VerificationService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("POSTGRES_DSN"), reason="durable conversation turns need PostgreSQL"
    ),
]

TENANT = "tenant-turns"
USER = "user-turns"
AGENT = "conversation-turns-agent"


@pytest.fixture
async def database() -> AsyncIterator[PostgresDatabase]:
    pytest.importorskip("asyncpg")
    db = PostgresDatabase(os.environ["POSTGRES_DSN"])
    await db.start()
    try:
        yield db
    finally:
        await db.close()


def build(
    database: PostgresDatabase, provider: MockModelProvider, calls: list[dict[str, Any]]
) -> tuple[ConversationService, AgentRuntime]:
    """A process's runtime and conversation service; everything durable is in PostgreSQL."""
    agents = InMemoryAgentRegistry()
    # Its own agent name: recovery in a shared database must not drive other tests' runs on it.
    agents.register(make_agent(name=AGENT, enabled_tools=frozenset({"api.write"})))
    tools = ToolRegistry()

    async def write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        calls.append(dict(arguments))
        return {"ok": True}

    tools.register(
        Tool(
            ToolDefinition(
                name="api.write",
                version="1",
                description="Write a record.",
                input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
                output_schema={"type": "object"},
                side_effect=SideEffect.WRITE,
                idempotency="keyed",
            ),
            write,
        )
    )
    policies = CompositePolicyEngine()
    router = ModelRouter(circuit_failure_threshold=1)
    router.register_provider(provider)
    memory = PostgresMemoryStore(database)
    audits = PostgresAuditLog(database)
    runtime = AgentRuntime(
        agents=agents,
        router=router,
        tools=tools,
        tool_executor=ToolExecutor(tools, policies),
        planners=PlannerRegistry(),
        contexts=ContextBuilderRegistry(DefaultContextBuilder(memory)),
        policies=policies,
        verifiers=VerificationService(),
        composers=ResponseComposerRegistry(),
        runs=PostgresRunStore(database),
        memory=memory,
        events=PostgresEventBus(database),
        approvals=PostgresApprovalService(database),
        audits=audits,
    )
    handlers = ConversationHandlerRegistry()
    handlers.register(RuntimeConversationHandler(runtime, delta_interval_seconds=0))
    service = ConversationService(
        PostgresConversationStore(database),
        PostgresConversationEventBus(database),
        handlers,
        audits,
    )
    return service, runtime


async def latest(
    service: ConversationService, conversation_id: str, *statuses: MessageStatus
) -> ConversationMessage:
    for _ in range(600):
        messages = await service.messages(conversation_id, TENANT)
        if messages and messages[-1].status in statuses:
            return messages[-1]
        await asyncio.sleep(0.01)
    raise AssertionError(f"message never reached {statuses}: {messages[-1].status}")


async def event_types(service: ConversationService, conversation_id: str, last: str) -> list[str]:
    """The conversation's event types, once ``last`` is the latest one."""
    for _ in range(600):
        history = [event.type for event in await service.events.history(conversation_id)]
        if history and history[-1] == last:
            return history
        await asyncio.sleep(0.01)
    raise AssertionError(f"the last event never became {last}: {history[-3:]}")


async def test_pending_turn_survives_restart_on_postgres_and_resumes_once(
    database: PostgresDatabase,
) -> None:
    calls: list[dict[str, Any]] = []
    provider = MockModelProvider(
        [tool_call_response("api.write", {"value": "draft"}), "saved and done"]
    )
    service, runtime = build(database, provider, calls)
    conversation = await service.create(tenant_id=TENANT, user_id=USER, agent=AGENT)
    await service.submit(conversation.id, TENANT, USER, "update it")
    paused = await latest(service, conversation.id, MessageStatus.AWAITING_APPROVAL)
    assert paused.pending is not None and paused.pending.approval_id
    await service.shutdown(0.1)
    await runtime.shutdown(0.1)

    restarted, restarted_runtime = build(database, provider, calls)
    await restarted_runtime.recover()
    await restarted.recover()  # the database may hold other tests' conversations
    stored = await latest(restarted, conversation.id, MessageStatus.AWAITING_APPROVAL)
    assert stored.pending == paused.pending
    with pytest.raises(ConflictError, match="waiting for approval"):
        await restarted.submit(conversation.id, TENANT, USER, "again")

    await restarted.respond(
        conversation.id,
        paused.pending.turn_id,
        TENANT,
        USER,
        ConversationApprovalDecision(decision="approved"),
    )
    done = await latest(restarted, conversation.id, MessageStatus.COMPLETED, MessageStatus.FAILED)

    assert done.id == paused.id
    assert done.status == MessageStatus.COMPLETED
    assert done.text_content == "saved and done"
    assert calls == [{"value": "draft"}]
    # The message is stored as completed before its completed event is written.
    history = await event_types(restarted, conversation.id, "assistant.message.completed")
    assert history.count("assistant.message.awaiting_approval") == 1
    assert history.count("assistant.message.resumed") == 1
    assert "assistant.message.delta" in history
    assert history[-1] == "assistant.message.completed"
    with pytest.raises(ConflictError):
        await restarted.respond(
            conversation.id,
            paused.pending.turn_id,
            TENANT,
            USER,
            ConversationApprovalDecision(decision="approved"),
        )
    assert calls == [{"value": "draft"}]
    # Nothing may still write once the fixture closes the pool.
    await restarted.shutdown(1)
    await restarted_runtime.shutdown(1)


# --- a real restart mid-turn ---------------------------------------------------------------------

HANG = "<hang>"


class HangingProvider(MockModelProvider):
    """Answers as scripted until a ``HANG`` reply, which blocks: the process dies mid-turn."""

    def __init__(self, responses: list[Any]) -> None:
        super().__init__(responses)
        self.hanging = asyncio.Event()

    async def generate(self, request: Any) -> Any:
        if self._responses and self._responses[0] == HANG:
            self.hanging.set()
            await asyncio.Event().wait()
        return await super().generate(request)


async def crashed_mid_turn(
    *responses: Any,
) -> tuple[str, ConversationMessage, list[dict[str, Any]]]:
    """Start a turn in a 'first process' with its own pool, then kill it mid-turn: its pool is
    closed under it, with no shutdown, so nothing it still does reaches the database."""
    calls: list[dict[str, Any]] = []
    first_db = PostgresDatabase(os.environ["POSTGRES_DSN"])
    await first_db.start()
    provider = HangingProvider([*responses, HANG])
    service, _ = build(first_db, provider, calls)
    conversation = await service.create(tenant_id=TENANT, user_id=USER, agent=AGENT)
    _, assistant = await service.submit(conversation.id, TENANT, USER, "which records?")
    await asyncio.wait_for(provider.hanging.wait(), 10)
    await first_db.close()
    return conversation.id, assistant, calls


async def message(
    service: ConversationService, conversation_id: str, message_id: str
) -> ConversationMessage:
    return next(m for m in await service.messages(conversation_id, TENANT) if m.id == message_id)


async def test_a_turn_killed_mid_run_completes_after_a_restart_on_postgres(
    database: PostgresDatabase,
) -> None:
    # The model hangs on its first call; the process is killed while it works.
    conversation_id, assistant, calls = await crashed_mid_turn()
    restarted, restarted_runtime = build(database, MockModelProvider(["the answer"]), calls)
    streaming = await message(restarted, conversation_id, assistant.id)
    assert streaming.status == MessageStatus.STREAMING and len(streaming.run_ids) == 1

    await restarted_runtime.recover()  # as Container.astart: runs first, then conversations
    await restarted.recover()
    done = await latest(restarted, conversation_id, MessageStatus.COMPLETED, MessageStatus.FAILED)

    assert done.id == assistant.id
    assert done.status == MessageStatus.COMPLETED, done.content
    assert done.text_content == "the answer"
    assert done.run_ids == streaming.run_ids
    assert calls == []
    history = await event_types(restarted, conversation_id, "assistant.message.completed")
    assert "assistant.message.failed" not in history
    await restarted.recover()
    assert (await message(restarted, conversation_id, assistant.id)).status == (
        MessageStatus.COMPLETED
    )
    await restarted.shutdown(1)
    await restarted_runtime.shutdown(1)


async def test_a_turn_killed_before_its_run_id_was_stored_finds_its_run_on_postgres(
    database: PostgresDatabase,
) -> None:
    conversation_id, assistant, calls = await crashed_mid_turn()
    restarted, restarted_runtime = build(database, MockModelProvider(["found"]), calls)
    streaming = await message(restarted, conversation_id, assistant.id)
    # As if the process died between Runtime.start and storing the run id on the message.
    await restarted.store.update_message(streaming.model_copy(update={"run_ids": ()}))

    await restarted_runtime.recover()
    await restarted.recover()
    done = await latest(restarted, conversation_id, MessageStatus.COMPLETED, MessageStatus.FAILED)
    assert done.status == MessageStatus.COMPLETED and done.text_content == "found"
    assert done.run_ids == streaming.run_ids
    await restarted.shutdown(1)
    await restarted_runtime.shutdown(1)


async def test_a_turn_whose_run_paused_while_down_awaits_approval_on_postgres(
    database: PostgresDatabase,
) -> None:
    calls: list[dict[str, Any]] = []
    provider = MockModelProvider(
        [tool_call_response("api.write", {"value": "draft"}), "saved and done"]
    )
    service, runtime = build(database, provider, calls)
    conversation = await service.create(tenant_id=TENANT, user_id=USER, agent=AGENT)
    await service.submit(conversation.id, TENANT, USER, "update it")
    paused = await latest(service, conversation.id, MessageStatus.AWAITING_APPROVAL)
    await service.shutdown(0.1)
    await runtime.shutdown(0.1)
    # The run paused, but the process died before the message left streaming.
    await service.store.update_message(
        paused.model_copy(update={"status": MessageStatus.STREAMING, "pending": None})
    )

    restarted, restarted_runtime = build(database, provider, calls)
    await restarted_runtime.recover()
    await restarted.recover()
    waiting = await latest(restarted, conversation.id, MessageStatus.AWAITING_APPROVAL)
    assert waiting.pending == paused.pending.model_copy(
        update={"paused_at": waiting.pending.paused_at}
    )
    await restarted.respond(
        conversation.id,
        waiting.pending.turn_id,
        TENANT,
        USER,
        ConversationApprovalDecision(decision="approved"),
    )
    done = await latest(restarted, conversation.id, MessageStatus.COMPLETED, MessageStatus.FAILED)
    assert done.text_content == "saved and done"
    assert calls == [{"value": "draft"}]
    await restarted.shutdown(1)
    await restarted_runtime.shutdown(1)


async def test_a_turn_whose_run_is_gone_fails_as_interrupted_on_postgres(
    database: PostgresDatabase,
) -> None:
    service, runtime = build(database, MockModelProvider([]), [])
    conversation = await service.create(tenant_id=TENANT, user_id=USER, agent=AGENT)
    user = ConversationMessage.text(
        conversation_id=conversation.id, tenant_id=TENANT, role="user", text="hello"
    )
    await service.store.append_message(user)
    assistant = ConversationMessage(
        conversation_id=conversation.id,
        tenant_id=TENANT,
        role="assistant",
        content=(),
        status=MessageStatus.STREAMING,
        reply_to_message_id=user.id,
        run_ids=(f"gone-{conversation.id}",),
    )
    await service.store.append_message(assistant)

    await service.recover()
    failed = await latest(service, conversation.id, MessageStatus.FAILED)
    assert failed.metadata == {"error_type": "InterruptedResponse", "retryable": True}
    await service.shutdown(1)
    await runtime.shutdown(1)


async def test_two_processes_recovering_one_turn_settle_it_once_on_postgres(
    database: PostgresDatabase,
) -> None:
    service, runtime = build(database, MockModelProvider(["only once"]), [])
    conversation = await service.create(tenant_id=TENANT, user_id=USER, agent=AGENT)
    await service.submit(conversation.id, TENANT, USER, "go")
    done = await latest(service, conversation.id, MessageStatus.COMPLETED)
    await service.shutdown(1)
    await runtime.shutdown(1)
    before = len(await service.events.history(conversation.id))
    await service.store.update_message(
        done.model_copy(update={"status": MessageStatus.STREAMING, "content": ()})
    )

    first, first_runtime = build(database, MockModelProvider([]), [])
    second, second_runtime = build(database, MockModelProvider([]), [])
    await asyncio.gather(first.recover(), second.recover())
    again = await latest(first, conversation.id, MessageStatus.COMPLETED)
    await asyncio.sleep(0.2)

    assert again.text_content == "only once"
    added = [event.type for event in (await first.events.history(conversation.id))[before:]]
    assert added.count("assistant.message.completed") == 1
    assert "assistant.message.failed" not in added
    for process, process_runtime in ((first, first_runtime), (second, second_runtime)):
        await process.shutdown(1)
        await process_runtime.shutdown(1)


async def test_a_deploy_mid_turn_detaches_and_the_next_start_completes_it_on_postgres(
    database: PostgresDatabase,
) -> None:
    calls: list[dict[str, Any]] = []
    first_db = PostgresDatabase(os.environ["POSTGRES_DSN"])
    await first_db.start()
    provider = HangingProvider([HANG])
    service, runtime = build(first_db, provider, calls)
    conversation = await service.create(tenant_id=TENANT, user_id=USER, agent=AGENT)
    _, assistant = await service.submit(conversation.id, TENANT, USER, "during a deploy")
    await asyncio.wait_for(provider.hanging.wait(), 10)
    # A graceful stop, in Container.aclose's order: conversations first, then the runtime.
    await service.shutdown(0.05)
    await runtime.shutdown(0.05)
    await first_db.close()

    restarted, restarted_runtime = build(database, MockModelProvider(["after the deploy"]), calls)
    left = await message(restarted, conversation.id, assistant.id)
    assert left.status == MessageStatus.STREAMING and left.run_ids
    state = await restarted_runtime.status(left.run_ids[0], TENANT)
    assert state.status.value not in {"cancelled", "failed", "completed"}
    await restarted_runtime.recover()
    await restarted.recover()
    done = await latest(restarted, conversation.id, MessageStatus.COMPLETED, MessageStatus.FAILED)
    assert done.status == MessageStatus.COMPLETED and done.text_content == "after the deploy"
    history = await event_types(restarted, conversation.id, "assistant.message.completed")
    assert "assistant.message.cancelled" not in history
    await restarted.shutdown(1)
    await restarted_runtime.shutdown(1)


async def test_concurrent_events_from_two_processes_get_distinct_sequences_on_postgres(
    database: PostgresDatabase,
) -> None:
    first, first_runtime = build(database, MockModelProvider([]), [])
    second, second_runtime = build(database, MockModelProvider([]), [])
    conversation = await first.create(tenant_id=TENANT, user_id=USER, agent=AGENT)
    await asyncio.gather(
        *(
            process._emit(conversation, "test.event", {"n": n})
            for n in range(30)
            for process in (first, second)
        )
    )
    sequences = [event.sequence for event in await first.events.history(conversation.id)]
    assert len(sequences) == 61
    assert sequences == list(range(1, 62))
    for process, process_runtime in ((first, first_runtime), (second, second_runtime)):
        await process.shutdown(1)
        await process_runtime.shutdown(1)

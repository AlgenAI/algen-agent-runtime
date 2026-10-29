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

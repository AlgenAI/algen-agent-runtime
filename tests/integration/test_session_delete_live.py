"""Deleting one session, and one user's runs and sessions, in Redis and PostgreSQL."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from conftest import make_runtime
from user_data_seed import seed_user

from algen_agent_runtime.approvals.service import InMemoryApprovalService
from algen_agent_runtime.conversations.feedback import PostgresConversationFeedbackStore
from algen_agent_runtime.conversations.service import (
    ConversationHandlerRegistry,
    ConversationService,
    RuntimeConversationHandler,
)
from algen_agent_runtime.conversations.stores import (
    PostgresConversationEventBus,
    PostgresConversationStore,
)
from algen_agent_runtime.events.bus import InMemoryEventBus
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.persistence import UserDataService
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.persistence.postgres import PostgresDatabase, PostgresMemoryStore
from algen_agent_runtime.persistence.quotas import StorageQuota, StorageQuotas, StorageQuotaSettings
from algen_agent_runtime.persistence.redis import RedisMemoryStore, RedisRunStore
from algen_agent_runtime.persistence.tool_executions import InMemoryToolExecutionStore
from algen_agent_runtime.types.contracts import Message, Role
from algen_agent_runtime.types.interfaces import SessionDeletion

pytestmark = [pytest.mark.integration]


@pytest.fixture
async def redis_client() -> AsyncIterator[Any]:
    if not os.environ.get("REDIS_URL"):
        pytest.skip("requires a live Redis")
    asyncio_redis = pytest.importorskip("redis.asyncio")
    client = asyncio_redis.Redis.from_url(os.environ["REDIS_URL"])
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def database() -> AsyncIterator[PostgresDatabase]:
    if not os.environ.get("POSTGRES_DSN"):
        pytest.skip("requires a live PostgreSQL")
    pytest.importorskip("asyncpg")
    db = PostgresDatabase(os.environ["POSTGRES_DSN"])
    await db.start()
    try:
        yield db
    finally:
        await db.close()


@pytest.mark.redis
@pytest.mark.parametrize("quota", [False, True])
async def test_redis_delete_session_removes_messages_owner_and_usage(
    redis_client: Any, quota: bool
) -> None:
    prefix = f"algen-test:{uuid4()}"
    quotas = StorageQuotas(StorageQuotaSettings(default=StorageQuota(memory_items=10)))
    store = RedisMemoryStore(
        redis_client, prefix=prefix, retention_seconds=60, quotas=quotas if quota else None
    )
    assert isinstance(store, SessionDeletion)
    tenant = "tenant[*]?"  # glob characters are matched literally
    assert await store.claim_session(tenant, "s1", "alice")
    assert await store.claim_session(tenant, "s2", "alice")
    assert await store.claim_session(tenant, "s3", "bob")
    await store.append(tenant, "s1", [Message.text(Role.USER, "a"), Message.text(Role.USER, "b")])
    await store.append(tenant, "s3", [Message.text(Role.USER, "c")])

    assert await store.sessions_for_user(tenant, "alice") == ("s1", "s2")
    assert await store.session_owner(tenant, "s3") == "bob"
    assert await store.delete_session(tenant, "s1") == 2
    assert await store.get(tenant, "s1", 10) == ()
    assert await store.session_owner(tenant, "s1") is None
    assert await store.sessions_for_user(tenant, "alice") == ("s2",)
    assert await store.delete_session(tenant, "s1") == 0
    if quota:
        assert (await store.tenant_usage(tenant)).items == 1
    assert await store.get(tenant, "s3", 10)


@pytest.mark.redis
async def test_redis_runs_and_memory_are_erased_per_user(redis_client: Any) -> None:
    prefix = f"algen-test:{uuid4()}"
    stores: dict[str, Any] = {
        "runs": RedisRunStore(redis_client, prefix=prefix),
        "memory": RedisMemoryStore(redis_client, prefix=prefix, retention_seconds=60),
        "events": InMemoryEventBus(),
        "approvals": InMemoryApprovalService(),
        "tool_executions": InMemoryToolExecutionStore(),
        "artifacts": InMemoryArtifactStore(),
    }
    alice = await seed_user(stores, "t", "alice")
    bob = await seed_user(stores, "t", "bob")
    service = UserDataService(**stores)

    assert [s.id for s in await stores["runs"].list_for_user("t", "alice")] == alice.run_ids
    report = await service.erase_user("t", "alice")

    assert report.counts["runs"] == 2
    assert report.counts["session_memory"] == 1 and report.counts["session_owners"] == 1
    assert await stores["runs"].list_for_user("t", "alice") == ()
    assert await stores["memory"].get("t", alice.session_id, 10) == ()
    assert [s.id for s in await stores["runs"].list_for_user("t", "bob")] == bob.run_ids
    assert await stores["memory"].get("t", bob.session_id, 10)
    assert (await service.erase_user("t", "alice")).total == 0


@pytest.mark.postgres
async def test_postgres_delete_session_removes_messages_and_owner(
    database: PostgresDatabase,
) -> None:
    tenant = f"tenant-{uuid4()}"
    store = PostgresMemoryStore(database, retention_seconds=3600)
    assert await store.claim_session(tenant, "s1", "alice")
    await store.append(tenant, "s1", [Message.text(Role.USER, "a"), Message.text(Role.USER, "b")])
    # Expired rows go too: readers skip them, but they still hold the user's words.
    await database.execute(
        "UPDATE algen_agent_runtime_memory SET expires_at = now() - interval '1 hour' "
        "WHERE tenant_id=$1 AND id = (SELECT min(id) FROM algen_agent_runtime_memory "
        "WHERE tenant_id=$1)",
        tenant,
    )

    assert await store.sessions_for_user(tenant, "alice") == ("s1",)
    assert await store.delete_session(tenant, "s1") == 2
    assert await store.session_owner(tenant, "s1") is None
    assert await store.delete_session(tenant, "s1") == 0
    assert await store.claim_session(tenant, "s1", "bob")


@pytest.mark.postgres
async def test_deleting_a_postgres_conversation_deletes_its_memory(
    database: PostgresDatabase,
) -> None:
    tenant = f"tenant-{uuid4()}"
    runtime = make_runtime(MockModelProvider(["the tool said 42"]))
    runtime.memory = PostgresMemoryStore(database, retention_seconds=3600)
    handlers = ConversationHandlerRegistry()
    handlers.register(RuntimeConversationHandler(runtime, stream=False))
    conversations = ConversationService(
        PostgresConversationStore(database),
        PostgresConversationEventBus(database),
        handlers,
        feedback_store=PostgresConversationFeedbackStore(database),
    )
    conversation = await conversations.create(tenant_id=tenant, user_id="alice", agent="test-agent")
    await conversations.submit(conversation.id, tenant, "alice", "what did the tool say?")
    for _ in range(400):
        messages = await conversations.messages(conversation.id, tenant)
        if messages and messages[-1].status.value == "completed":
            break
        await asyncio.sleep(0.01)
    assert await runtime.memory.get(tenant, conversation.id, 10)

    await conversations.delete(conversation.id, tenant, "alice")

    row = await database.fetchrow(
        "SELECT (SELECT count(*) FROM algen_agent_runtime_memory WHERE tenant_id=$1) AS memory, "
        "(SELECT count(*) FROM algen_agent_runtime_session_owners WHERE tenant_id=$1) AS owners",
        tenant,
    )
    assert (row["memory"], row["owners"]) == (0, 0)

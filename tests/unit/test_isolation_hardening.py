"""Tenant, user and secret isolation at the edges: the in-memory tool ledger, the Redis turn
index, persistent vector stores, the tool-result cache default, configuration errors and
mandatory policies that fail."""

from __future__ import annotations

import fnmatch
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from algen_agent_runtime.cache.contracts import CacheScope
from algen_agent_runtime.config.settings import AppSettings, RetrievalSettings, load_settings
from algen_agent_runtime.exceptions.errors import ConfigurationError, ConflictError
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.persistence.redis import RedisRunStore
from algen_agent_runtime.persistence.tool_executions import InMemoryToolExecutionStore
from algen_agent_runtime.policies.contracts import (
    POLICY_ERROR_REASON_CODE,
    PolicyAction,
    PolicyDecision,
    PolicyErrorHandling,
    PolicyFailMode,
    PolicyPoint,
)
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.retrieval.contracts import SourceDocument
from algen_agent_runtime.retrieval.memory import HashingEmbedder
from algen_agent_runtime.retrieval.vector_stores import ChromaRetriever
from algen_agent_runtime.tools.contracts import ToolResult
from algen_agent_runtime.tools.testing import fake_tool_context
from algen_agent_runtime.types.contracts import RunRequest, RunState, RunStatus

# --- GOV-12: the in-memory ledger never hands one tenant another's record --------------------


async def test_in_memory_ledger_refuses_another_tenants_idempotency_key() -> None:
    store = InMemoryToolExecutionStore()
    alice = fake_tool_context(tenant_id="tenant-a", idempotency_key="run-1:call-1")
    record, started = await store.begin(alice, "billing.charge")
    assert started
    await store.complete(record.idempotency_key, ToolResult(value={"card": "tenant-a data"}))

    mallory = fake_tool_context(tenant_id="tenant-b", idempotency_key="run-1:call-1")
    with pytest.raises(ConflictError):
        await store.begin(mallory, "billing.charge")
    assert await store.get("run-1:call-1", "tenant-b") is None
    # The owner still replays its own record.
    replayed, again = await store.begin(alice, "billing.charge")
    assert not again and replayed.result is not None


# --- DATA-5: the Redis turn lookup stays inside the tenant ---------------------------------


class _Redis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def scan_iter(self, match: str = "*", count: int = 1000) -> AsyncIterator[str]:
        for key in list(self.data):
            if fnmatch.fnmatchcase(key, match):
                yield key


def _turn_run(run_id: str, tenant_id: str) -> RunState:
    return RunState(
        id=run_id,
        request=RunRequest(
            agent="test-agent",
            input="hello",
            tenant_id=tenant_id,
            user_id="user-1",
            conversation_id="conv-1",
            turn_id="t-1",
        ),
        status=RunStatus.COMPLETED,
        started_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.mark.redis
async def test_redis_find_by_turn_escapes_the_tenant_and_checks_the_state() -> None:
    redis = _Redis()
    store = RedisRunStore(redis, prefix="p")
    await store.create(_turn_run("run-a", "tenant-a"))
    # Runs created before the turn index: found by scanning the tenant's runs.
    redis.data = {key: value for key, value in redis.data.items() if ":turn-run:" not in key}

    assert (await store.find_by_turn("tenant-a", "conv-1", "t-1")) is not None
    # A tenant id that is a glob pattern does not match another tenant's keys.
    assert await store.find_by_turn("tenant-*", "conv-1", "t-1") is None
    assert await store.find_by_turn("*", "conv-1", "t-1") is None

    # A run stored under one tenant's key but owned by another is not returned.
    foreign = _turn_run("run-b", "tenant-b")
    redis.data["p:tenant:tenant-c:run:run-b"] = foreign.model_dump_json()
    redis.data["p:tenant:tenant-c:turn-run:conv-1:t-1"] = "run-b"
    assert await store.find_by_turn("tenant-c", "conv-1", "t-1") is None
    del redis.data["p:tenant:tenant-c:turn-run:conv-1:t-1"]
    assert await store.find_by_turn("tenant-c", "conv-1", "t-1") is None


# --- DATA-7: persistent vector stores need a tenant on every document ----------------------


class _Collection:
    def __init__(self) -> None:
        self.records: dict[str, Any] = {}

    def delete(self, **_: Any) -> None:
        return None

    def upsert(self, ids: list[str], **_: Any) -> None:
        for item in ids:
            self.records[item] = True


class _Chroma:
    def __init__(self) -> None:
        self.collection = _Collection()

    def get_or_create_collection(self, name: str, metadata: object) -> _Collection:
        return self.collection


async def test_persistent_vector_stores_refuse_documents_without_a_tenant() -> None:
    client = _Chroma()
    retriever = ChromaRetriever(HashingEmbedder(), client=client, chunker=None)
    with pytest.raises(ValueError, match="no tenant_id"):
        await retriever.ingest(
            (
                SourceDocument(id="ok", text="a", source="s", metadata={"tenant_id": "t"}),
                SourceDocument(id="orphan", text="b", source="s"),
            )
        )
    # Nothing was written: the batch is checked before any embedding or write.
    assert client.collection.records == {}


# --- DATA-6: tool results are cached per user unless the operator says otherwise -----------


def _cache_settings(policy: Mapping[str, Any]) -> AppSettings:
    return AppSettings.model_validate(
        {"cache": {"backend": "memory", "policies": {"tool_results": policy}}}
    )


def test_tool_result_cache_defaults_to_user_scope() -> None:
    container = build_container(_cache_settings({"enabled": True}))
    assert container.cache.policy("tool_results").scope == CacheScope.USER
    explicit = build_container(_cache_settings({"enabled": True, "scope": "tenant"}))
    assert explicit.cache.policy("tool_results").scope == CacheScope.TENANT


# --- DATA-3 / DATA-4: configuration never echoes a secret ----------------------------------


def test_configuration_errors_do_not_print_rejected_values() -> None:
    secret = "-".join(("sk", "LIVE", "do-not-print"))
    with pytest.raises(ConfigurationError) as raised:
        load_settings((), {"providers": {"openai": {"type": "openai", "api_key": secret}}})
    assert secret not in str(raised.value)
    assert "providers.openai" in str(raised.value)
    with pytest.raises(ConfigurationError) as raised:
        load_settings((), {"storage": {"postgres_dsn": f"postgresql://u:{secret}@h/db"}})
    assert secret not in str(raised.value)
    assert raised.value.__cause__ is None


def test_retrieval_connection_url_with_a_password_must_be_an_env_reference() -> None:
    with pytest.raises(ValueError, match="contains a password"):
        RetrievalSettings(type="pgvector", connection_url="postgresql://u:pw@db/app")
    with pytest.raises(ValueError, match="contains a password"):
        RetrievalSettings(type="qdrant", mode="vector", connection_url="https://u:pw@q:6333")
    assert RetrievalSettings(type="pgvector", connection_url="env://PGVECTOR_DSN")
    assert RetrievalSettings(type="pgvector", connection_url="postgresql://localhost/example")
    assert RetrievalSettings(type="qdrant", mode="vector", connection_url="http://q:6333")


# --- GOV-11: a mandatory policy never fails open --------------------------------------------


class _Broken:
    category = "test"
    enforcement_mode = "block"
    applies_to = frozenset({PolicyPoint.PLAN_TOOL, PolicyPoint.BEFORE_TOOL})

    def __init__(self, name: str) -> None:
        self.name = name

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        raise RuntimeError("broken")


@pytest.mark.parametrize("name", ["authorization", "side_effects", "custom"])
async def test_a_mandatory_policy_fails_closed_even_when_the_boundary_fails_open(
    name: str,
) -> None:
    engine = CompositePolicyEngine(
        (_Broken(name),), error_handling=PolicyErrorHandling(on_error=PolicyFailMode.OPEN)
    )
    if name == "custom":
        engine.register(_Broken("bundle"), mandatory=True)
    decision = await engine.evaluate("plan_tool", {}, {})
    if name == "custom":
        # The non-mandatory policy fails open; the mandatory one still refuses.
        assert decision.audit_metadata["policy_errors"][0]["fail_mode"] == "open"
    assert decision.action == PolicyAction.DENY
    assert decision.reason_code == POLICY_ERROR_REASON_CODE
    assert decision.audit_metadata["fail_mode"] == "closed"

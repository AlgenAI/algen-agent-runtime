"""Per-tenant storage quotas on the in-memory stores, the API and session memory writes."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.config.settings import AppSettings, StorageSettings
from algen_agent_runtime.exceptions.errors import QuotaExceededError
from algen_agent_runtime.memory.contracts import LearnedMemory, MemoryMutation, MemoryScope
from algen_agent_runtime.memory.store import InMemoryLearnedMemoryStore
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore, InMemoryMemoryStore
from algen_agent_runtime.persistence.quotas import (
    ResourceLimit,
    ResourceUsage,
    StorageQuota,
    StorageQuotas,
    StorageQuotaSettings,
)
from algen_agent_runtime.types.contracts import (
    Artifact,
    Message,
    Role,
    RunRequest,
    RunStatus,
    utc_now,
)


def _quotas(**default: Any) -> StorageQuotas:
    return StorageQuotas(StorageQuotaSettings(default=StorageQuota(**default)))


def _artifact(tenant: str, size: int, artifact_id: str | None = None) -> Artifact:
    values: dict[str, Any] = {
        "tenant_id": tenant,
        "name": "a.bin",
        "media_type": "application/octet-stream",
        "data": b"x" * size,
    }
    if artifact_id is not None:
        values["id"] = artifact_id
    return Artifact(**values)


def test_tenant_overrides_merge_over_the_default() -> None:
    quotas = StorageQuotas(
        StorageQuotaSettings.model_validate(
            {
                "default": {"artifact_bytes": 100, "memory_items": 10},
                "tenants": {
                    "big": {"artifact_bytes": 1_000},
                    "free": {"memory_items": None},
                },
            }
        )
    )
    assert quotas.limit("other", "artifacts") == ResourceLimit(None, 100)
    assert quotas.limit("big", "artifacts") == ResourceLimit(None, 1_000)
    assert quotas.limit("big", "memory") == ResourceLimit(10, None)
    assert quotas.limit("free", "memory").unlimited
    assert quotas.configured
    assert not StorageQuotas().configured
    assert not StorageQuotas().enforced("any", "artifacts")


def test_check_allows_writes_that_do_not_grow_usage_past_the_limit() -> None:
    quotas = _quotas(artifact_count=2)
    quotas.check("t", "artifacts", ResourceUsage(1, 0), ResourceUsage(2, 0))
    # Over the limit already (the quota was lowered): shrinking or staying put is allowed.
    quotas.check("t", "artifacts", ResourceUsage(5, 0), ResourceUsage(4, 0))
    with pytest.raises(QuotaExceededError) as raised:
        quotas.check("t", "artifacts", ResourceUsage(2, 0), ResourceUsage(3, 0))
    assert raised.value.details() == {
        "code": "quota_exceeded",
        "resource": "artifacts",
        "dimension": "items",
        "limit": 2,
        "used": 3,
    }


async def test_artifact_quota_counts_and_bytes_per_tenant() -> None:
    store = InMemoryArtifactStore(quotas=_quotas(artifact_count=2, artifact_bytes=150))
    await store.put(_artifact("a", 100))
    with pytest.raises(QuotaExceededError) as raised:
        await store.put(_artifact("a", 60))
    assert (raised.value.dimension, raised.value.used) == ("bytes", 160)
    await store.put(_artifact("a", 50))
    with pytest.raises(QuotaExceededError):
        await store.put(_artifact("a", 1))
    # Another tenant has its own usage.
    await store.put(_artifact("b", 150))
    assert await store.tenant_usage("a") == ResourceUsage(2, 150)
    # Deleting frees room.
    listed = await store.list("a")
    assert await store.delete(listed[0].id, "a")
    await store.put(_artifact("a", 10))


async def test_replacing_an_artifact_counts_only_the_difference() -> None:
    store = InMemoryArtifactStore(quotas=_quotas(artifact_count=1, artifact_bytes=100))
    await store.put(_artifact("a", 90, "same"))
    await store.put(_artifact("a", 100, "same"))
    assert await store.tenant_usage("a") == ResourceUsage(1, 100)


async def test_session_memory_quota() -> None:
    store = InMemoryMemoryStore(_quotas(memory_items=3))
    await store.append("a", "s1", [Message.text(Role.USER, "1"), Message.text(Role.USER, "2")])
    with pytest.raises(QuotaExceededError):
        await store.append("a", "s2", [Message.text(Role.USER, "3"), Message.text(Role.USER, "4")])
    assert len(await store.get("a", "s2", 10)) == 0
    await store.append("a", "s2", [Message.text(Role.USER, "3")])
    assert (await store.tenant_usage("a")).items == 3
    await store.delete("a", "s1")
    assert (await store.tenant_usage("a")).items == 1


def _mutation(scope: MemoryScope, key: str, revision: int, content: str | None) -> MemoryMutation:
    record = LearnedMemory(
        key=key,
        revision=revision,
        kind="preference" if content is not None else None,
        content=content,
        evidence="the user said so" if content is not None else None,
        deleted=content is None,
        source_run_id="run",
        source_step_id="step",
        source_agent_version="1.0.0",
        updated_at=utc_now(),
        expires_at=utc_now() + timedelta(days=1) if content is not None else None,
    )
    return MemoryMutation(
        scope=scope,
        operation_id=f"{key}-{revision}",
        expected_revision=revision - 1,
        record=record,
    )


async def test_learned_memory_quota_counts_records_with_content() -> None:
    store = InMemoryLearnedMemoryStore(_quotas(learned_memory_items=1))
    scope = MemoryScope(tenant_id="a", user_id="u", agent_id="agent")
    await store.apply(_mutation(scope, "first", 1, "likes tea"))
    with pytest.raises(QuotaExceededError):
        await store.apply(_mutation(scope, "second", 1, "likes coffee"))
    # Updating the counted record is fine; deleting it makes room.
    await store.apply(_mutation(scope, "first", 2, "likes green tea"))
    await store.apply(_mutation(scope, "first", 3, None))
    assert await store.tenant_usage("a") == ResourceUsage()
    await store.apply(_mutation(scope, "second", 1, "likes coffee"))
    assert (await store.tenant_usage("a")).items == 1


async def test_a_full_memory_quota_keeps_the_answer_but_not_the_turn() -> None:
    runtime = make_runtime(MockModelProvider(["answer"]), make_agent())
    # A turn writes two messages (question and answer); the quota has room for one.
    runtime.memory = InMemoryMemoryStore(_quotas(memory_items=1))

    result = await runtime.run(
        RunRequest(agent="test-agent", input="hi", tenant_id="t", user_id="u")
    )

    assert result.status == RunStatus.COMPLETED
    assert result.output == "answer"
    events = await runtime.events.history(result.run_id)
    [written] = [event.data for event in events if event.type == "memory.written"]
    assert written["operation"] == "skipped"
    assert written["reason"] == "quota_exceeded"
    [audit] = [
        audit
        for audit in await runtime.audits.list("t")
        if audit.action == "storage.quota_exceeded"
    ]
    assert audit.outcome == "refused"
    assert audit.metadata["resource"] == "memory"


def _headers(tenant: str = "tenant", scopes: str = "artifacts:write usage:read") -> dict[str, str]:
    return {"x-tenant-id": tenant, "x-user-id": "user", "x-scopes": scopes}


async def test_api_answers_413_past_the_quota_and_reports_usage() -> None:
    settings = AppSettings(
        storage=StorageSettings(
            quotas=StorageQuotaSettings(default=StorageQuota(artifact_count=1, artifact_bytes=64))
        )
    )
    container = build_container(settings)
    transport = httpx.ASGITransport(app=create_app(settings, container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/artifacts?name=a.txt&media_type=text/plain", headers=_headers(), content=b"hello"
        )
        assert created.status_code == 201
        refused = await client.post(
            "/v1/artifacts?name=b.txt&media_type=text/plain", headers=_headers(), content=b"again"
        )
        assert refused.status_code == 413
        body = refused.json()
        assert body["code"] == "QUOTA_EXCEEDED"
        assert (body["resource"], body["dimension"], body["limit"], body["used"]) == (
            "artifacts",
            "items",
            1,
            2,
        )

        usage = await client.get("/v1/storage/usage", headers=_headers())
        assert usage.status_code == 200
        assert usage.json() == {
            "tenant_id": "tenant",
            "resources": {
                "artifacts": {"items": 1, "bytes": 5, "items_limit": 1, "bytes_limit": 64},
                "memory": {"items": 0, "bytes": 0, "items_limit": None, "bytes_limit": None},
                "learned_memory": {
                    "items": 0,
                    "bytes": 0,
                    "items_limit": None,
                    "bytes_limit": None,
                },
            },
        }
        denied = await client.get("/v1/storage/usage", headers=_headers(scopes="artifacts:read"))
        assert denied.status_code == 403

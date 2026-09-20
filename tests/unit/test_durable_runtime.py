from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_runtime
from httpx import AsyncClient
from pydantic import ValidationError
from starlette.requests import Request

from algen_agent_runtime.api.dependencies import principal_dependency
from algen_agent_runtime.config.settings import ApiSettings, AppSettings, StorageSettings
from algen_agent_runtime.events.bus import PostgresEventBus
from algen_agent_runtime.exceptions.errors import ToolExecutionError
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.persistence.postgres import (
    PostgresArtifactStore,
    PostgresDatabase,
    PostgresRunStore,
)
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.types.contracts import RunRequest, RunState, RunStatus, utc_now


def test_safe_error_exposes_only_runtime_optional_dependency_guidance() -> None:
    assert AgentRuntime._safe_error(ImportError("install algen-agent-runtime[pgvector]")) == (
        "install algen-agent-runtime[pgvector]"
    )
    assert AgentRuntime._safe_error(ImportError("No module named '/private/path'")) == (
        "Internal error (ImportError)"
    )


def test_postgres_backends_require_secret_reference() -> None:
    with pytest.raises(ValidationError, match="postgres_dsn"):
        StorageSettings(run_store="postgres")
    with pytest.raises(ValidationError, match="env://"):
        StorageSettings(run_store="postgres", postgres_dsn="postgresql://literal")


def test_initial_schema_uses_only_algen_agent_runtime_names() -> None:
    directory = Path(__file__).parents[2] / "src/algen_agent_runtime/persistence/migrations"
    initial = (directory / "001_initial.sql").read_text(encoding="utf-8")
    conversations = (directory / "002_conversations.sql").read_text(encoding="utf-8")
    assert "agent_core" not in initial + conversations
    assert "algen_agent_runtime_runs" in initial
    assert "algen_agent_runtime_conversation_messages" in conversations


def test_container_wires_selected_postgres_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_POSTGRES_DSN", "postgresql://runtime:test@localhost/runtime")
    settings = AppSettings.model_validate(
        {
            "storage": {
                "run_store": "postgres",
                "memory_store": "postgres",
                "event_store": "postgres",
                "audit_store": "postgres",
                "approval_store": "postgres",
                "artifact_store": "postgres",
                "tool_execution_store": "postgres",
                "postgres_dsn": "env://TEST_POSTGRES_DSN",
            }
        }
    )
    container = build_container(settings)
    assert isinstance(container.runtime.runs, PostgresRunStore)
    assert isinstance(container.events, PostgresEventBus)
    assert isinstance(container.artifacts, PostgresArtifactStore)
    assert any(isinstance(resource, PostgresDatabase) for resource in container.resources)
    assert any(isinstance(resource, AsyncClient) for resource in container.resources)


async def test_completed_tool_execution_is_replayed_without_reexecution() -> None:
    calls = 0

    async def handler(arguments, context):
        nonlocal calls
        calls += 1
        return {"value": arguments["value"]}

    registry = ToolRegistry()
    registry.register(
        Tool(
            ToolDefinition(
                name="write.record",
                version="1.0.0",
                description="write a record",
                input_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                side_effect=SideEffect.WRITE,
                idempotency=Idempotency.KEYED,
            ),
            handler,
        )
    )
    executor = ToolExecutor(registry, SimpleNamespace(evaluate=_allow))
    context = _tool_context("replay-key")
    first = await executor.execute("write.record", {"value": "x"}, context)
    second = await executor.execute("write.record", {"value": "x"}, context)
    assert first == second
    assert calls == 1


async def test_interrupted_side_effect_is_not_replayed() -> None:
    calls = 0

    async def handler(arguments, context):
        nonlocal calls
        calls += 1
        raise TimeoutError

    registry = ToolRegistry()
    registry.register(
        Tool(
            ToolDefinition(
                name="write.external",
                version="1.0.0",
                description="write externally",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                side_effect=SideEffect.EXTERNAL,
                retry_policy={"max_attempts": 1},
            ),
            handler,
        )
    )
    executor = ToolExecutor(registry, SimpleNamespace(evaluate=_allow))
    context = _tool_context("uncertain-key")
    with pytest.raises(ToolExecutionError, match="timed out"):
        await executor.execute("write.external", {}, context)
    with pytest.raises(ToolExecutionError, match="manual reconciliation"):
        await executor.execute("write.external", {}, context)
    assert calls == 1


async def test_runtime_recovers_interrupted_model_checkpoint() -> None:
    runtime = make_runtime()
    request = RunRequest(agent="test-agent", input="hello", tenant_id="tenant", user_id="user")
    state = RunState(
        request=request,
        agent_key="test-agent@1.0.0",
        status=RunStatus.INVOKING_MODEL,
        deadline=utc_now() + timedelta(seconds=10),
    )
    await runtime.runs.create(state)
    assert await runtime.recover() == 1
    for _ in range(100):
        current = await runtime.status(state.id, "tenant")
        if current.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
            break
        await asyncio.sleep(0.001)
    assert current.status == RunStatus.COMPLETED


async def test_shutdown_keeps_interrupted_run_recoverable() -> None:
    class SlowProvider(MockModelProvider):
        async def generate(self, request):
            await asyncio.sleep(60)
            return await super().generate(request)

    runtime = make_runtime(provider=SlowProvider(["answer"]))
    state = await runtime.start(
        RunRequest(agent="test-agent", input="hello", tenant_id="tenant", user_id="user")
    )
    for _ in range(100):
        if (await runtime.status(state.id, "tenant")).status == RunStatus.INVOKING_MODEL:
            break
        await asyncio.sleep(0.001)
    await runtime.shutdown(grace_seconds=0)
    current = await runtime.status(state.id, "tenant")
    assert current.status not in {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.TIMED_OUT,
    }


async def test_jwt_principal_uses_verified_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeJwtError(Exception):
        pass

    fake_jwt = SimpleNamespace(
        PyJWTError=FakeJwtError,
        decode=lambda token, key, **options: {
            "sub": "user-1",
            "tenant_id": "tenant-1",
            "scope": "runs:read runs:write",
            "exp": 4_000_000_000,
        },
    )
    monkeypatch.setitem(sys.modules, "jwt", fake_jwt)
    monkeypatch.setenv("TEST_JWT_KEY", "verification-key")
    dependency = principal_dependency(ApiSettings(auth_mode="jwt", jwt_key="env://TEST_JWT_KEY"))
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"authorization", b"Bearer signed-token")],
        }
    )
    identity = await dependency(request)
    assert identity.tenant_id == "tenant-1"
    assert identity.user_id == "user-1"
    assert identity.scopes == {"runs:read", "runs:write"}


async def test_development_principal_has_no_implicit_scopes() -> None:
    dependency = principal_dependency(ApiSettings())
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-tenant-id", b"tenant-1"), (b"x-user-id", b"user-1")],
        }
    )
    identity = await dependency(request)
    assert identity.scopes == frozenset()


async def _allow(point, payload, context):
    return SimpleNamespace(action="allow", reason="allowed", value=payload)


def _tool_context(key: str) -> ToolContext:
    return ToolContext(
        run_id="run-1",
        step_id="step-1",
        tenant_id="tenant-1",
        user_id="user-1",
        permissions=frozenset(),
        idempotency_key=key,
    )

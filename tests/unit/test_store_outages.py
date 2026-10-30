"""Store outages and concurrent drives: what the persistence drills found, on in-memory stores.

The live versions of these are in tests/drills (PostgreSQL and Redis paused and restarted).
"""

from __future__ import annotations

import asyncio
import sys
import types
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
from conftest import make_agent, make_runtime
from httpx import ASGITransport, AsyncClient

from algen_agent_runtime.config.settings import AppSettings, StorageSettings
from algen_agent_runtime.exceptions.errors import (
    ConflictError,
    RunConflictError,
    StoreUnavailableError,
)
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.persistence import postgres as postgres_module
from algen_agent_runtime.persistence.memory import InMemoryRunStore
from algen_agent_runtime.persistence.postgres import PostgresDatabase, _unavailable
from algen_agent_runtime.persistence.redis import RedisRunStore, redis_unavailable
from algen_agent_runtime.runtime import runtime as runtime_module
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.types.contracts import (
    ApprovalPolicy,
    ErrorKind,
    RunRequest,
    RunState,
    RunStatus,
    utc_now,
)


def request(**updates: Any) -> RunRequest:
    values: dict[str, Any] = {
        "agent": "test-agent",
        "input": "go",
        "tenant_id": "t",
        "user_id": "u",
    }
    values.update(updates)
    return RunRequest(**values)


# --- Driver errors become StoreUnavailableError ----------------------------------------------


class _DriverError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"sqlstate {sqlstate}")
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    "error",
    [
        _DriverError("08006"),  # connection failure
        _DriverError("08003"),  # connection does not exist (closed mid-operation)
        _DriverError("57P01"),  # admin shutdown
        _DriverError("57P03"),  # cannot connect now (starting up)
        _DriverError("53300"),  # too many connections
        ConnectionRefusedError(61, "refused"),
        TimeoutError(),
    ],
)
def test_postgres_unavailability_is_recognised(error: Exception) -> None:
    unavailable = _unavailable(error)
    assert isinstance(unavailable, StoreUnavailableError)
    assert unavailable.error_kind is ErrorKind.UNAVAILABLE and unavailable.retryable
    # Content-minimal: the error type, never the driver's message (which may hold a DSN).
    assert "sqlstate" not in str(unavailable)


@pytest.mark.parametrize("error", [_DriverError("23505"), _DriverError("42P01"), ValueError()])
def test_other_postgres_errors_pass_through(error: Exception) -> None:
    assert _unavailable(error) is None


class _FailingPool:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.expired = 0

    async def execute(self, query: str, *arguments: Any) -> str:
        raise self.error

    fetchrow = fetch = execute

    async def expire_connections(self) -> None:
        self.expired += 1

    @asynccontextmanager
    async def acquire(self) -> Any:
        raise self.error
        yield

    async def close(self) -> None:
        return None


async def test_a_lost_connection_raises_store_unavailable_and_renews_the_pool() -> None:
    database = PostgresDatabase("postgresql://db")
    pool = _FailingPool(_DriverError("08003"))
    database._pool = pool
    for call in (database.execute, database.fetchrow, database.fetch):
        with pytest.raises(StoreUnavailableError) as raised:
            await call("SELECT 1")
        assert isinstance(raised.value.__cause__, _DriverError)
    with pytest.raises(StoreUnavailableError):
        async with database.transaction():
            pass
    # Every idle connection is replaced, so one call meets a dead pool, not one per connection.
    assert pool.expired == 4


async def test_a_statement_error_is_not_turned_into_unavailability() -> None:
    database = PostgresDatabase("postgresql://db")
    pool = _FailingPool(_DriverError("23505"))
    database._pool = pool
    with pytest.raises(_DriverError):
        await database.execute("INSERT")
    assert pool.expired == 0


async def test_the_pool_is_created_with_connect_and_command_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def create_pool(dsn: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return _FailingPool(ValueError())

    module = types.ModuleType("asyncpg")
    module.create_pool = create_pool  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "asyncpg", module)
    database = PostgresDatabase(
        "postgresql://db",
        initialize_schema=False,
        connect_timeout_seconds=3,
        command_timeout_seconds=7,
    )
    await database.start()
    assert seen["timeout"] == 3 and seen["command_timeout"] == 7


async def test_an_unreachable_server_at_start_raises_store_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def create_pool(dsn: str, **kwargs: Any) -> Any:
        raise ConnectionRefusedError(61, "Connect call failed ('10.0.0.1', 5432)")

    module = types.ModuleType("asyncpg")
    module.create_pool = create_pool  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "asyncpg", module)
    with pytest.raises(StoreUnavailableError, match="PostgreSQL is unavailable"):
        await PostgresDatabase("postgresql://db").start()


async def test_migrations_are_not_held_to_the_command_timeout() -> None:
    executed: list[dict[str, Any]] = []

    class Connection:
        async def execute(self, query: str, *arguments: Any, **options: Any) -> str:
            executed.append(options)
            return "OK"

        async def fetch(self, query: str) -> list[Any]:
            return []

        @asynccontextmanager
        async def transaction(self) -> Any:
            yield

    await PostgresDatabase("postgresql://db", command_timeout_seconds=1)._migrate(Connection())
    migrations = [options for options in executed if options]
    assert migrations and all(
        options["timeout"] == postgres_module._MIGRATION_TIMEOUT_SECONDS for options in migrations
    )


def _redis_error(name: str) -> Exception:
    # redis is an optional extra: an error shaped like redis-py's, by module and class name.
    base = type("RedisError", (Exception,), {"__module__": "redis.exceptions"})
    return type(name, (base,), {"__module__": "redis.exceptions"})("Error 61 connecting")


@pytest.mark.parametrize("name", ["ConnectionError", "TimeoutError", "BusyLoadingError"])
def test_redis_unavailability_is_recognised(name: str) -> None:
    assert isinstance(redis_unavailable(_redis_error(name)), StoreUnavailableError)


def test_other_redis_errors_pass_through() -> None:
    assert redis_unavailable(_redis_error("ResponseError")) is None
    assert redis_unavailable(ConnectionError()) is None  # the builtin, not redis-py's


async def test_redis_store_calls_raise_store_unavailable() -> None:
    error = _redis_error("ConnectionError")

    class Down:
        async def get(self, key: str) -> Any:
            raise error

        async def set(self, *arguments: Any, **options: Any) -> Any:
            raise error

    store = RedisRunStore(Down())
    with pytest.raises(StoreUnavailableError):
        await store.get("run", "t")
    with pytest.raises(StoreUnavailableError):
        await store.create(RunState(request=request()))


# --- Configuration ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", ["event_store", "approval_store", "tool_execution_store"])
def test_postgres_stores_that_reference_runs_need_postgres_runs(store: str) -> None:
    with pytest.raises(ValueError, match="require run_store 'postgres'"):
        StorageSettings(
            run_store="redis",
            redis_url="env://REDIS_URL",
            postgres_dsn="env://DATABASE_URL",
            **{store: "postgres"},
        )
    StorageSettings(run_store="postgres", postgres_dsn="env://DATABASE_URL", **{store: "postgres"})


def test_timeouts_have_bounded_defaults() -> None:
    storage = StorageSettings()
    assert storage.postgres_connect_timeout_seconds == 10
    assert storage.postgres_command_timeout_seconds == 60
    assert storage.redis_socket_timeout_seconds == 10
    assert (
        StorageSettings(postgres_command_timeout_seconds=None).postgres_command_timeout_seconds
        is None
    )


async def test_the_api_answers_503_while_a_store_is_unavailable() -> None:
    from algen_agent_runtime.api.app import create_app
    from algen_agent_runtime.orchestration.container import build_container

    settings = AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
            "agents": [
                {
                    "name": "worker",
                    "version": "1.0.0",
                    "description": "Works",
                    "system_instructions": "Work.",
                    "default_model": {"name": "d", "provider": "mock", "model": "deterministic"},
                }
            ],
        }
    )
    container = build_container(settings)

    async def down(*arguments: Any, **options: Any) -> Any:
        raise StoreUnavailableError("PostgreSQL is unavailable (ConnectionRefusedError)")

    container.runtime.runs.create = down  # type: ignore[method-assign]
    app = create_app(settings, container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            "/v1/runs",
            json={"agent": "worker", "input": "hi"},
            headers={"x-tenant-id": "t", "x-user-id": "u", "x-scopes": "*"},
        )
    assert response.status_code == 503
    assert response.json() == {
        "detail": "PostgreSQL is unavailable (ConnectionRefusedError)",
        "kind": "unavailable",
    }
    assert response.headers["retry-after"]


# --- The runtime through an outage ------------------------------------------------------------


class FlakyRunStore(InMemoryRunStore):
    """An in-memory run store that is down while ``down`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise StoreUnavailableError("PostgreSQL is unavailable (ConnectionRefusedError)")

    async def create(self, state: RunState) -> None:
        self._check()
        await super().create(state)

    async def get(self, run_id: str, tenant_id: str) -> RunState | None:
        self._check()
        return await super().get(run_id, tenant_id)

    async def save(self, state: RunState, expected_version: int) -> None:
        self._check()
        await super().save(state, expected_version)


class GatedProvider(MockModelProvider):
    """Holds the model call numbered ``hold`` (1-based) until ``opened`` is set."""

    def __init__(self, *responses: Any, hold: int = 1) -> None:
        super().__init__(list(responses))
        self.hold = hold
        self.calls = 0
        self.entered = asyncio.Event()
        self.opened = asyncio.Event()

    async def generate(self, model_request: Any) -> Any:
        self.calls += 1
        if self.calls == self.hold and not self.opened.is_set():
            self.entered.set()
            await self.opened.wait()
        return await super().generate(model_request)


def _write_tool(calls: list[str]) -> Tool:
    async def write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        calls.append(context.idempotency_key)
        return {"ok": True}

    return Tool(
        ToolDefinition(
            name="api.write",
            version="1",
            description="Write.",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            side_effect=SideEffect.WRITE,
            idempotency="keyed",
        ),
        write,
    )


async def test_a_run_interrupted_by_an_outage_is_not_failed_and_resumes_once_the_store_is_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "_OUTAGE_RETRY_SECONDS", (0.01, 0.05))
    calls: list[str] = []
    provider = GatedProvider(tool_call_response("api.write", {}), "done", hold=2)
    runtime = make_runtime(
        provider,
        make_agent(
            enabled_tools=frozenset({"api.write"}),
            approval_policy=ApprovalPolicy(require_for_side_effects=False),
        ),
    )
    runtime.tools.register(_write_tool(calls))
    store = FlakyRunStore()
    runtime.runs = store
    state = await runtime.start(request(), credentials={"authorization": "Bearer x"})
    await asyncio.wait_for(provider.entered.wait(), 5)

    store.down = True
    provider.opened.set()
    for _ in range(200):
        if state.id in runtime._outage_runs:
            break
        await asyncio.sleep(0.01)
    assert state.id in runtime._outage_runs
    # The drive keeps the caller's credentials for the next drive of this run.
    assert runtime._outage_runs[state.id][1] == {"authorization": "Bearer x"}
    await asyncio.sleep(0.1)  # still down: retried, not failed
    store.down = False

    result = await asyncio.wait_for(runtime.wait(state.id, "t"), 5)
    assert result.status is RunStatus.COMPLETED, result.error
    assert len(calls) == 1
    assert runtime._outage_runs == {}
    assert runtime._credentials == {}


async def test_shutdown_leaves_outage_interrupted_runs_for_the_next_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "_OUTAGE_RETRY_SECONDS", (10.0, 10.0))
    provider = GatedProvider("done")
    runtime = make_runtime(provider)
    store = FlakyRunStore()
    runtime.runs = store
    state = await runtime.start(request())
    await asyncio.wait_for(provider.entered.wait(), 5)
    store.down = True
    provider.opened.set()
    for _ in range(200):
        if state.id in runtime._outage_runs:
            break
        await asyncio.sleep(0.01)
    task = runtime._outage_task
    assert task is not None
    await runtime.shutdown(0.1)
    assert task.done() and runtime._outage_runs == {}
    store.down = False
    # Not failed: its checkpoint is what the next start recovers.
    assert (await store.get(state.id, "t")).status not in {  # type: ignore[union-attr]
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }


async def test_a_superseded_drive_stands_down_without_failing_the_run() -> None:
    provider = GatedProvider("done")
    runtime = make_runtime(provider)
    state = await runtime.start(request())
    await asyncio.wait_for(provider.entered.wait(), 5)
    # Another process's drive moves the run on meanwhile.
    current = await runtime.runs.get(state.id, "t")
    assert current is not None
    current.status = RunStatus.PLANNING
    await runtime.runs.save(current, current.version)

    provider.opened.set()
    result = await asyncio.wait_for(runtime.wait(state.id, "t"), 5)
    assert result.status is RunStatus.PLANNING  # left to the other drive, not failed
    assert result.error is None


def test_run_stores_raise_run_conflict_error_on_a_stale_version() -> None:
    assert issubclass(RunConflictError, ConflictError)


async def test_in_memory_run_store_raises_run_conflict_error() -> None:
    store = InMemoryRunStore()
    state = RunState(request=request())
    await store.create(state)
    with pytest.raises(RunConflictError):
        await store.save(state, state.version + 1)


async def test_a_recovered_step_is_counted_once() -> None:
    baseline = make_runtime()
    finished = await baseline.run(request())
    assert finished.status is RunStatus.COMPLETED
    expected = (await baseline.status(finished.run_id, "t")).step_count

    runtime = make_runtime()
    state = RunState(
        request=request(),
        agent_key="test-agent@1.0.0",
        status=RunStatus.INVOKING_MODEL,
        step_count=1,  # the model step was counted, then the process died
        deadline=utc_now() + timedelta(seconds=10),
    )
    await runtime.runs.create(state)
    assert await runtime.recover() == 1
    result = await runtime.wait(state.id, "t")
    assert result.status is RunStatus.COMPLETED
    assert (await runtime.status(state.id, "t")).step_count == expected


async def test_repeated_recovery_does_not_use_up_the_step_budget() -> None:
    runtime = make_runtime(agent=make_agent(max_steps=2))
    state = RunState(
        request=request(),
        agent_key="test-agent@1.0.0",
        status=RunStatus.PLANNING,
        deadline=utc_now() + timedelta(seconds=10),
    )
    await runtime.runs.create(state)
    for _ in range(5):  # five crashes, each in the middle of the same model call
        current = await runtime.runs.get(state.id, "t")
        assert current is not None
        current.step_count += 1
        current.status = RunStatus.INVOKING_MODEL
        await runtime.runs.save(current, current.version)
        runtime._rewind_for_recovery(current)
        await runtime.runs.save(current, current.version)
    assert (await runtime.runs.get(state.id, "t")).step_count == 0  # type: ignore[union-attr]
    assert await runtime.recover() == 1
    result = await runtime.wait(state.id, "t")
    assert result.status is RunStatus.COMPLETED, result.error


# --- Resuming right after the pause -----------------------------------------------------------


async def _paused_run(calls: list[str]) -> tuple[Any, str]:
    runtime = make_runtime(
        MockModelProvider([tool_call_response("api.write", {}), "done"]),
        make_agent(enabled_tools=frozenset({"api.write"})),
    )
    runtime.tools.register(_write_tool(calls))
    state = await runtime.start(request())
    for _ in range(500):
        if (await runtime.status(state.id, "t")).status is RunStatus.AWAITING_APPROVAL:
            break
        await asyncio.sleep(0.001)
    return runtime, state.id


async def test_resume_waits_for_the_drive_that_paused_the_run() -> None:
    calls: list[str] = []
    runtime, run_id = await _paused_run(calls)
    await asyncio.sleep(0.05)
    # The pausing drive is still finishing its tail when the approver answers.
    tail = asyncio.create_task(asyncio.sleep(0.1))
    runtime._tasks[run_id] = tail
    await runtime.resume(run_id, "t", {"decision": "approved"})
    result = await asyncio.wait_for(runtime.wait(run_id, "t"), 5)
    assert result.status is RunStatus.COMPLETED, result.error
    assert len(calls) == 1


async def test_resume_of_a_run_still_executing_decides_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "_PAUSED_DRIVE_WAIT_SECONDS", 0.01)
    calls: list[str] = []
    runtime, run_id = await _paused_run(calls)
    await asyncio.sleep(0.05)
    stuck = asyncio.create_task(asyncio.Event().wait())
    runtime._tasks[run_id] = stuck
    try:
        with pytest.raises(ConflictError, match="already executing"):
            await runtime.resume(run_id, "t", {"decision": "approved"})
        state = await runtime.status(run_id, "t")
        approval = await runtime.approvals.get(state.pause_payload["approval_id"], "t")
        assert approval is not None and approval.status.value == "pending"
    finally:
        stuck.cancel()

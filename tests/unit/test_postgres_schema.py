"""An optional PostgreSQL schema for the Runtime's tables (``storage.postgres_schema``)."""

from __future__ import annotations

import sys
import types
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from structlog.testing import capture_logs

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.persistence.postgres import PostgresDatabase, check_schema_name


class _FakePool:
    def __init__(
        self,
        *,
        existing: set[str],
        search_path: str | None,
        usable: bool = True,
        legacy_public: bool = False,
    ) -> None:
        self.existing = existing
        self.search_path = search_path
        self.usable = usable
        self.legacy_public = legacy_public
        self.executed: list[str] = []
        self.locks: list[str] = []
        self.closed = False

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_FakePool]:
        yield self

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "pg_advisory" in query:
            self.locks.append(query.split("(")[0].removeprefix("SELECT "))
            return True
        if "pg_namespace" in query:
            return 1 if args[0] in self.existing else None
        if "current_schema()" in query:
            name = (self.search_path or '"public"').strip('"')
            return name if name in self.existing and self.usable else None
        if "to_regclass" in query:
            return self.legacy_public
        raise AssertionError(query)

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append(query)
        if query.startswith("CREATE SCHEMA"):
            self.existing.add(query.split('"')[1])
        return "OK"

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        return []

    async def close(self) -> None:
        self.closed = True


def _fake_asyncpg(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing: set[str],
    honour_search_path: bool = True,
    usable: bool = True,
    legacy_public: bool = False,
) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def create_pool(dsn: str, **kwargs: Any) -> _FakePool:
        seen["kwargs"] = kwargs
        settings = kwargs.get("server_settings") or {}
        pool = _FakePool(
            existing=existing,
            search_path=settings.get("search_path") if honour_search_path else None,
            usable=usable,
            legacy_public=legacy_public,
        )
        seen["pool"] = pool
        return pool

    module = types.ModuleType("asyncpg")
    module.create_pool = create_pool  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "asyncpg", module)
    return seen


async def test_schema_sets_search_path_and_creates_a_missing_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _fake_asyncpg(monkeypatch, existing={"public"})
    database = PostgresDatabase("postgresql://db", schema="lms_agent")
    migrated: list[Any] = []

    async def migrate(pool: Any) -> None:
        migrated.append(pool)

    monkeypatch.setattr(database, "_migrate", migrate)

    await database.start()

    assert seen["kwargs"]["server_settings"] == {"search_path": '"lms_agent"'}
    assert seen["pool"].executed == ['CREATE SCHEMA IF NOT EXISTS "lms_agent"']
    assert database.schema == "lms_agent"
    # The schema is in place before the migrations run on the same connection, and both happen
    # under the migration lock.
    assert migrated == [seen["pool"]]
    assert seen["pool"].locks == ["pg_advisory_lock", "pg_advisory_unlock"]


async def test_existing_schema_is_used_without_create(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_asyncpg(monkeypatch, existing={"public", "lms_agent"})
    database = PostgresDatabase("postgresql://db", schema="lms_agent", initialize_schema=False)

    await database.start()

    assert seen["pool"].executed == []
    assert not seen["pool"].closed


async def test_missing_schema_without_initialization_fails_and_closes_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _fake_asyncpg(monkeypatch, existing={"public"})
    database = PostgresDatabase("postgresql://db", schema="lms_agent", initialize_schema=False)

    with pytest.raises(ConfigurationError, match="does not exist"):
        await database.start()

    assert seen["pool"].closed
    assert database._pool is None


async def test_a_dropped_search_path_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_asyncpg(monkeypatch, existing={"public", "lms_agent"}, honour_search_path=False)
    database = PostgresDatabase("postgresql://db", schema="lms_agent", initialize_schema=False)

    with pytest.raises(ConfigurationError, match="not applied"):
        await database.start()

    assert seen["pool"].closed


async def test_a_schema_the_role_cannot_use_names_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_asyncpg(monkeypatch, existing={"public", "lms_agent"}, usable=False)
    database = PostgresDatabase("postgresql://db", schema="lms_agent", initialize_schema=False)

    with pytest.raises(ConfigurationError, match="lacks USAGE"):
        await database.start()


async def test_runtime_tables_left_in_public_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_asyncpg(monkeypatch, existing={"public", "lms_agent"}, legacy_public=True)
    database = PostgresDatabase("postgresql://db", schema="lms_agent")

    async def migrate(connection: Any) -> None:
        return None

    monkeypatch.setattr(database, "_migrate", migrate)

    with capture_logs() as logs:
        await database.start()

    warning = next(
        log
        for log in logs
        if log["event"] == "postgres_schema_is_empty_but_public_has_runtime_tables"
    )
    assert warning["log_level"] == "warning"
    assert warning["postgres_schema"] == "lms_agent"
    assert warning["legacy_schema"] == "public"


async def test_a_schema_that_already_holds_the_tables_logs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_asyncpg(monkeypatch, existing={"public", "lms_agent"}, legacy_public=False)
    database = PostgresDatabase("postgresql://db", schema="lms_agent", initialize_schema=False)

    with capture_logs() as logs:
        await database.start()

    assert not [log for log in logs if log["log_level"] == "warning"]


async def test_without_schema_the_pool_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_asyncpg(monkeypatch, existing={"public"})
    database = PostgresDatabase("postgresql://db", initialize_schema=False)

    await database.start()

    assert "server_settings" not in seen["kwargs"]
    assert seen["pool"].executed == []
    assert database.schema is None


@pytest.mark.parametrize(
    "name",
    ["Agent", "pg_agent", "information_schema", "lms-agent", "1agent", "", "a" * 64, 'x"; --'],
)
def test_unsafe_schema_names_are_refused(name: str) -> None:
    with pytest.raises(ConfigurationError):
        check_schema_name(name)
    with pytest.raises(ConfigurationError):
        PostgresDatabase("postgresql://db", schema=name)
    with pytest.raises(ValueError, match="postgres_schema"):
        AppSettings.model_validate({"storage": {"postgres_schema": name}})


def test_settings_pass_the_schema_to_the_shared_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_POSTGRES_DSN", "postgresql://db")
    settings = AppSettings.model_validate(
        {
            "storage": {
                "run_store": "postgres",
                "postgres_dsn": "env://TEST_POSTGRES_DSN",
                "postgres_schema": "lms_agent",
            }
        }
    )
    assert check_schema_name("_agent_runtime_2") == "_agent_runtime_2"
    assert AppSettings().storage.postgres_schema is None

    container = build_container(settings)

    database = container.runtime.runs._database  # type: ignore[attr-defined]
    assert isinstance(database, PostgresDatabase)
    assert database.schema == "lms_agent"

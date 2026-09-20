from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.contracts import ToolContext
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry
from examples.pattern_text_to_sql.app import (
    AGENT_BY_PROVIDER,
    EXAMPLE_DIR,
    ReadOnlyAnalyticsDatabase,
    ask,
    create_query_tool,
    parse_args,
)


def tool_context() -> ToolContext:
    return ToolContext(
        run_id="run",
        step_id="step",
        tenant_id="tenant",
        user_id="user",
        permissions=frozenset({"database.read"}),
        idempotency_key="run:query-1",
    )


async def test_text_to_sql_example_configuration_and_query() -> None:
    settings = load_settings((EXAMPLE_DIR / "agent.yaml",))
    agents = {agent.name: agent for agent in settings.agents}
    assert agents[AGENT_BY_PROVIDER["openai"]].default_model.provider == "openai"
    assert agents[AGENT_BY_PROVIDER["mistral"]].default_model.provider == "mistral"
    assert agents[AGENT_BY_PROVIDER["mistral"]].default_model.model == "ministral-3b-2512"
    assert agents[AGENT_BY_PROVIDER["mistral"]].model_allowlist == frozenset(
        {"mistral/ministral-3b-2512"}
    )
    assert agents[AGENT_BY_PROVIDER["mistral"]].default_model.extensions["temperature"] == 0
    assert "orders.status = 'completed'" in agents[AGENT_BY_PROVIDER["mistral"]].system_instructions
    assert settings.agents[0].enabled_tools == frozenset({"analytics.query"})

    database = ReadOnlyAnalyticsDatabase()
    registry = ToolRegistry()
    registry.register(create_query_tool(database))
    executor = ToolExecutor(registry, CompositePolicyEngine())
    try:
        result = await executor.execute(
            "analytics.query",
            {
                "statement": (
                    "SELECT c.region, SUM(o.total_amount) AS revenue "
                    "FROM customers c JOIN orders o ON o.customer_id = c.id "
                    "WHERE o.status = :status GROUP BY c.region ORDER BY revenue DESC"
                ),
                "parameters": {"status": "completed"},
            },
            tool_context(),
        )
    finally:
        database.close()

    assert result.value["rows"][0] == {"region": "North", "revenue": 4840.5}
    assert result.value["truncated"] is False


def test_text_to_sql_cli_selects_mistral() -> None:
    arguments = parse_args(["--provider", "mistral", "Show revenue by region"])
    assert arguments.provider == "mistral"
    assert arguments.question == "Show revenue by region"


def test_text_to_sql_cli_defaults_to_openai() -> None:
    assert parse_args([]).provider == "openai"


async def test_text_to_sql_closes_database_and_observability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    class FakeDatabase:
        def close(self) -> None:
            closed.append("database")

    class FakeTools:
        def register(self, tool: Any) -> None:
            del tool

    class FakeContainer:
        runtime = object()
        tools = FakeTools()

        def close(self) -> None:
            closed.append("container")

    class FakeClient:
        def __init__(self, runtime: Any) -> None:
            del runtime

        async def run(self, request: Any) -> Any:
            del request
            return SimpleNamespace(error=None, output="answer")

    monkeypatch.setattr("examples.pattern_text_to_sql.app.ReadOnlyAnalyticsDatabase", FakeDatabase)
    monkeypatch.setattr(
        "examples.pattern_text_to_sql.app.create_query_tool", lambda database: object()
    )
    monkeypatch.setattr(
        "examples.pattern_text_to_sql.app.build_container", lambda settings: FakeContainer()
    )
    monkeypatch.setattr("examples.pattern_text_to_sql.app.AlgenAgentRuntimeClient", FakeClient)

    assert await ask("question", "mistral") == "answer"
    assert closed == ["database", "container"]


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM orders",
        "DROP TABLE customers",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM customers; SELECT * FROM orders",
        "PRAGMA table_info(customers)",
    ],
)
async def test_text_to_sql_example_rejects_unsafe_sql(statement: str) -> None:
    database = ReadOnlyAnalyticsDatabase()
    try:
        with pytest.raises((ValueError, sqlite3.DatabaseError)):
            await database.query(statement, {}, tool_context())
    finally:
        database.close()

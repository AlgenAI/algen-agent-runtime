from __future__ import annotations

import argparse
import asyncio
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.models.providers.mock import tool_call_response
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)
from algen_agent_runtime.types.contracts import RunRequest
from examples.offline import offline_settings, require_key, script

EXAMPLE_DIR = Path(__file__).resolve().parent
ALLOWED_TABLES = frozenset({"customers", "orders"})
MAX_ROWS = 200
ProviderName = Literal["openai", "mistral"]
AGENT_BY_PROVIDER: dict[ProviderName, str] = {
    "openai": "text-to-sql",
    "mistral": "text-to-sql-mistral",
}

DENIED_SQLITE_ACTIONS = frozenset(
    action
    for action in (
        getattr(sqlite3, "SQLITE_INSERT", None),
        getattr(sqlite3, "SQLITE_UPDATE", None),
        getattr(sqlite3, "SQLITE_DELETE", None),
        getattr(sqlite3, "SQLITE_CREATE_INDEX", None),
        getattr(sqlite3, "SQLITE_CREATE_TABLE", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_INDEX", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_TABLE", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_VIEW", None),
        getattr(sqlite3, "SQLITE_CREATE_TRIGGER", None),
        getattr(sqlite3, "SQLITE_CREATE_VIEW", None),
        getattr(sqlite3, "SQLITE_DROP_INDEX", None),
        getattr(sqlite3, "SQLITE_DROP_TABLE", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_INDEX", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TABLE", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_VIEW", None),
        getattr(sqlite3, "SQLITE_DROP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_DROP_VIEW", None),
        getattr(sqlite3, "SQLITE_ALTER_TABLE", None),
        getattr(sqlite3, "SQLITE_ATTACH", None),
        getattr(sqlite3, "SQLITE_DETACH", None),
        getattr(sqlite3, "SQLITE_PRAGMA", None),
        getattr(sqlite3, "SQLITE_TRANSACTION", None),
        getattr(sqlite3, "SQLITE_SAVEPOINT", None),
    )
    if action is not None
)


class ReadOnlyAnalyticsDatabase:
    """Small SQLite adapter that enforces read-only, allowlisted query execution."""

    def __init__(self, schema_path: Path = EXAMPLE_DIR / "schema.sql") -> None:
        self._schema_path = schema_path
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        # Connect on first use, so constructing the adapter (as a plugin does) opens nothing.
        if self._connection is None:
            connection = sqlite3.connect(":memory:", check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.executescript(self._schema_path.read_text(encoding="utf-8"))
            connection.set_authorizer(self._authorize)
            self._connection = connection
        return self._connection

    @staticmethod
    def _authorize(
        action: int,
        argument_one: str | None,
        argument_two: str | None,
        database_name: str | None,
        trigger_name: str | None,
    ) -> int:
        del argument_two, database_name, trigger_name
        if action in DENIED_SQLITE_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ and argument_one not in ALLOWED_TABLES:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    async def query(
        self,
        statement: str,
        parameters: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        del context
        normalized = statement.strip().rstrip(";").strip()
        if not normalized:
            raise ValueError("SQL statement cannot be empty")
        if ";" in normalized:
            raise ValueError("only one SQL statement is allowed")
        first_keyword = normalized.split(maxsplit=1)[0].upper()
        if first_keyword not in {"SELECT", "WITH"}:
            raise ValueError("only SELECT or WITH queries are allowed")

        bounded_query = f"SELECT * FROM ({normalized}) AS agent_query LIMIT {MAX_ROWS + 1}"
        async with self._lock:
            return await asyncio.to_thread(self._execute, bounded_query, parameters)

    def _execute(self, statement: str, parameters: dict[str, Any]) -> dict[str, Any]:
        cursor = self._connect().execute(statement, parameters)
        rows = cursor.fetchmany(MAX_ROWS + 1)
        truncated = len(rows) > MAX_ROWS
        visible_rows = rows[:MAX_ROWS]
        columns = [description[0] for description in cursor.description or ()]
        return {
            "columns": columns,
            "rows": [dict(row) for row in visible_rows],
            "row_count": len(visible_rows),
            "truncated": truncated,
        }

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


def create_query_tool(database: ReadOnlyAnalyticsDatabase) -> Tool:
    async def execute(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        return await database.query(
            arguments["statement"], arguments.get("parameters", {}), context
        )

    return Tool(
        ToolDefinition(
            name="analytics.query",
            version="1.0.0",
            description=(
                "Execute one read-only SQLite SELECT or WITH query against customers and orders. "
                "Named parameters such as :region may be supplied separately. Returns columns, rows, "
                "row_count, and a truncation flag. Results are capped at 200 rows."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "statement": {
                        "type": "string",
                        "minLength": 1,
                        "description": "One SQLite SELECT or WITH statement.",
                    },
                    "parameters": {
                        "type": "object",
                        "description": "Values for named SQL parameters, without the leading colon.",
                    },
                },
                "required": ["statement"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "rows": {"type": "array", "items": {"type": "object"}},
                    "row_count": {"type": "integer", "minimum": 0},
                    "truncated": {"type": "boolean"},
                },
                "required": ["columns", "rows", "row_count", "truncated"],
                "additionalProperties": False,
            },
            required_permissions=frozenset({"database.read"}),
            side_effect=SideEffect.READ,
            idempotency=Idempotency.IDEMPOTENT,
            timeout_seconds=5,
            max_concurrency=4,
            max_result_bytes=262_144,
            audit_metadata={"data_source": "example-sqlite", "access": "read-only"},
        ),
        execute,
    )


KEY_BY_PROVIDER: dict[ProviderName, str] = {
    "openai": "OPENAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}
# Offline, the scripted model always asks for completed revenue by region, then answers. The query
# still runs through analytics.query, its schema check, the SQLite authorizer and the row limit.
OFFLINE_QUERY = (
    "SELECT c.region, SUM(o.total_amount) AS revenue FROM customers c "
    "JOIN orders o ON o.customer_id = c.id WHERE o.status = :status "
    "GROUP BY c.region ORDER BY revenue DESC"
)
OFFLINE_ANSWER = (
    f"The North region has the highest completed order revenue.\n\n```sql\n{OFFLINE_QUERY}\n```"
)


async def ask(question: str, provider: ProviderName = "openai", *, offline: bool = False) -> str:
    # The settings declare AnalyticsPlugin (plugin.py), which registers analytics.query and
    # hands its database to the container to close.
    config = (EXAMPLE_DIR / "agent.yaml",)
    settings = offline_settings(config, AGENT_BY_PROVIDER) if offline else load_settings(config)
    container = build_container(settings)
    if offline:
        script(
            container,
            provider,
            tool_call_response(
                "analytics.query",
                {"statement": OFFLINE_QUERY, "parameters": {"status": "completed"}},
            ),
            OFFLINE_ANSWER,
        )
    try:
        result = await AlgenAgentRuntimeClient(container.runtime).run(
            RunRequest(
                agent=AGENT_BY_PROVIDER[provider],
                input=question,
                tenant_id="example-tenant",
                user_id="example-user",
            )
        )
    finally:
        await container.aclose()
    if result.error:
        raise RuntimeError(result.error)
    return result.output or ""


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ask the OpenAI- or Mistral-backed Text-to-SQL Agent a question."
    )
    parser.add_argument(
        "question",
        nargs="?",
        default="Which region has the highest completed order revenue?",
    )
    parser.add_argument(
        "--provider",
        choices=tuple(AGENT_BY_PROVIDER),
        default="openai",
        help="Model provider to use (default: openai).",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use the deterministic mock provider with a scripted query; needs no API key.",
    )
    return parser.parse_args(arguments)


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parse_args(argv)
    if arguments.offline:
        print("[offline: scripted mock model; it always asks for completed revenue by region]")
    else:
        require_key(KEY_BY_PROVIDER[arguments.provider])
    print(asyncio.run(ask(arguments.question, arguments.provider, offline=arguments.offline)))


if __name__ == "__main__":
    main()

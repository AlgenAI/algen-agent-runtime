"""The Runtime's PostgreSQL schema, for hosts that create it with their own migration tool.

A host whose database role cannot run DDL at start-up (``storage.initialize_schema: false``)
applies the Runtime's migrations itself. ``migrations()`` and ``migration_sql()`` give it the
SQL the Runtime would run, in order (the ``algen-agent-runtime migrations`` command prints it),
and ``check_schema()`` compares a live database with what those migrations create, so a missing
table or column is found in the host's test suite or a deploy check, not by a failing store call.

Nothing here needs asyncpg except ``check_schema`` given a DSN.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.persistence.naming import check_schema_name

MIGRATIONS_DIRECTORY = Path(__file__).with_name("migrations")
# What the migrations create, as check_schema compares it. Regenerated from a database migrated
# from scratch: POSTGRES_DSN=... python scripts/schema_catalog.py --update.
CATALOG_PATH = Path(__file__).with_name("schema_catalog.json")
MIGRATIONS_TABLE = "algen_agent_runtime_schema_migrations"
_MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    """One of the Runtime's SQL migrations, as packaged."""

    name: str
    number: int
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def migrations() -> tuple[Migration, ...]:
    """The Runtime's migrations in the order it applies them (by file name)."""
    found: list[Migration] = []
    for path in sorted(MIGRATIONS_DIRECTORY.glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise RuntimeError(f"unexpected migration file name {path.name!r}")
        found.append(Migration(path.name, int(match.group(1)), path))
    return tuple(found)


def migration_sql(
    *, since: int | None = None, schema: str | None = None, record: bool = True
) -> str:
    """The SQL of the migrations after number ``since`` (all of them by default), in order.

    With ``schema``, the script first creates that schema if it is missing and sets
    ``search_path`` to it, as the Runtime does for ``storage.postgres_schema``. With ``record``
    (the default), each migration is followed by the row the Runtime writes to its history table
    ``algen_agent_runtime_schema_migrations``, so a Runtime later started with
    ``initialize_schema: true`` does not run it again. Run each migration in a transaction, as the
    Runtime does.
    """
    selected = [item for item in migrations() if since is None or item.number > since]
    parts: list[str] = []
    if schema is not None:
        name = check_schema_name(schema)
        parts.append(f'CREATE SCHEMA IF NOT EXISTS "{name}";\nSET search_path TO "{name}";\n')
    if record and selected:
        parts.append(
            f"CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());\n"
        )
    for item in selected:
        body = item.sql.rstrip()
        if not body.endswith(";"):
            body += ";"
        parts.append(f"-- algen-agent-runtime migration {item.name}\n{body}\n")
        if record:
            parts.append(
                f"INSERT INTO {MIGRATIONS_TABLE} (name) VALUES ('{item.name}') "
                "ON CONFLICT (name) DO NOTHING;\n"
            )
    return "\n".join(parts)


SchemaIssueKind = Literal[
    "missing_table",
    "missing_column",
    "type_mismatch",
    "not_null_mismatch",
    "missing_default",
    "missing_index",
    "missing_trigger",
]


class SchemaIssue(BaseModel):
    """One difference between a live schema and what the Runtime's migrations create."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: SchemaIssueKind
    table: str
    column: str | None = None
    name: str | None = None
    expected: str | None = None
    actual: str | None = None

    def __str__(self) -> str:
        where = self.table + (f".{self.column}" if self.column else "")
        if self.name:
            where += f" ({self.name})"
        detail = ""
        if self.expected is not None or self.actual is not None:
            detail = f": expected {self.expected}, found {self.actual}"
        return f"{self.kind} {where}{detail}"


class SchemaReport(BaseModel):
    """What ``check_schema`` found. ``ok`` when nothing the Runtime needs is missing.

    Tables, columns and indexes the host added are never reported. ``unrecorded_migrations``
    lists migrations missing from the Runtime's history table: informational, since a host that
    owns the schema may not keep that table, but a Runtime started with
    ``initialize_schema: true`` would run them again.
    """

    model_config = ConfigDict(extra="forbid")
    schema_name: str
    runtime_version: str
    latest_migration: str
    issues: list[SchemaIssue] = Field(default_factory=list)
    unrecorded_migrations: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def raise_for_issues(self) -> None:
        """Raise ``AssertionError`` naming every issue, for a host's test suite."""
        if self.issues:
            lines = "\n".join(f"  - {issue}" for issue in self.issues)
            raise AssertionError(
                f"PostgreSQL schema {self.schema_name!r} differs from algen-agent-runtime "
                f"{self.runtime_version} (latest migration {self.latest_migration}):\n{lines}\n"
                "Apply the missing migrations: algen-agent-runtime migrations sql --since NNN"
            )


_COLUMNS = """
SELECT c.relname AS table_name, a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       a.attnotnull AS not_null, a.atthasdef AS has_default
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE n.nspname = $1 AND c.relkind IN ('r', 'p') AND c.relname LIKE 'algen\\_agent\\_runtime\\_%'
ORDER BY c.relname, a.attnum
"""
_INDEXES = """
SELECT t.relname AS table_name, i.relname AS index_name
FROM pg_catalog.pg_index x
JOIN pg_catalog.pg_class i ON i.oid = x.indexrelid
JOIN pg_catalog.pg_class t ON t.oid = x.indrelid
JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname = $1 AND t.relname LIKE 'algen\\_agent\\_runtime\\_%'
ORDER BY t.relname, i.relname
"""
_TRIGGERS = """
SELECT c.relname AS table_name, g.tgname AS trigger_name
FROM pg_catalog.pg_trigger g
JOIN pg_catalog.pg_class c ON c.oid = g.tgrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = $1 AND NOT g.tgisinternal AND c.relname LIKE 'algen\\_agent\\_runtime\\_%'
ORDER BY c.relname, g.tgname
"""


async def introspect(fetch: Any, schema: str) -> dict[str, Any]:
    """The Runtime's tables in ``schema`` as a catalog: columns (type, NOT NULL, default),
    indexes and triggers. ``fetch(query, *args)`` runs a query, as asyncpg's does."""
    tables: dict[str, dict[str, Any]] = {}
    for row in await fetch(_COLUMNS, schema):
        table = tables.setdefault(
            str(row["table_name"]), {"columns": {}, "indexes": [], "triggers": []}
        )
        table["columns"][str(row["column_name"])] = {
            "type": str(row["data_type"]),
            "not_null": bool(row["not_null"]),
            "has_default": bool(row["has_default"]),
        }
    for query, key, column in (
        (_INDEXES, "indexes", "index_name"),
        (_TRIGGERS, "triggers", "trigger_name"),
    ):
        for row in await fetch(query, schema):
            owner = tables.get(str(row["table_name"]))
            if owner is not None:
                owner[key].append(str(row[column]))
    return {"tables": dict(sorted(tables.items()))}


def expected_catalog() -> dict[str, Any]:
    """What the Runtime's migrations create (the packaged catalog)."""
    catalog: dict[str, Any] = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return catalog


def compare_catalogs(expected: dict[str, Any], actual: dict[str, Any]) -> list[SchemaIssue]:
    """The issues of ``actual`` against ``expected``; additions in ``actual`` are not issues."""
    issues: list[SchemaIssue] = []
    actual_tables = actual.get("tables", {})
    for table, wanted in expected.get("tables", {}).items():
        if table == MIGRATIONS_TABLE:
            continue  # bookkeeping of initialize_schema; reported as unrecorded_migrations
        found = actual_tables.get(table)
        if found is None:
            issues.append(SchemaIssue(kind="missing_table", table=table))
            continue
        for column, spec in wanted["columns"].items():
            have = found["columns"].get(column)
            if have is None:
                issues.append(
                    SchemaIssue(
                        kind="missing_column", table=table, column=column, expected=spec["type"]
                    )
                )
                continue
            if have["type"] != spec["type"]:
                issues.append(
                    SchemaIssue(
                        kind="type_mismatch",
                        table=table,
                        column=column,
                        expected=spec["type"],
                        actual=have["type"],
                    )
                )
            # A column the Runtime leaves NULL must accept NULL; one it requires may be looser.
            if have["not_null"] and not spec["not_null"]:
                issues.append(
                    SchemaIssue(
                        kind="not_null_mismatch",
                        table=table,
                        column=column,
                        expected="nullable",
                        actual="NOT NULL",
                    )
                )
            # The Runtime's inserts leave out columns that have a default.
            if spec["has_default"] and not have["has_default"]:
                issues.append(
                    SchemaIssue(
                        kind="missing_default",
                        table=table,
                        column=column,
                        expected="a default",
                        actual="none",
                    )
                )
        for key, kind in (("indexes", "missing_index"), ("triggers", "missing_trigger")):
            present = set(found.get(key, ()))
            for name in wanted.get(key, ()):
                if name not in present:
                    issues.append(SchemaIssue(kind=kind, table=table, name=name))  # type: ignore[arg-type]
    return issues


async def check_schema(target: Any, *, schema: str | None = None) -> SchemaReport:
    """Compare a live PostgreSQL schema with what the Runtime's migrations create.

    ``target`` is a DSN (``postgresql://...``; ``env://VAR`` is resolved first, and asyncpg is
    imported only then), a ``PostgresDatabase``, or anything with asyncpg's
    ``fetch(query, *args)`` (a connection or pool). ``schema`` is the schema holding the tables
    (``storage.postgres_schema``); by default the database's ``postgres_schema`` when ``target``
    is a ``PostgresDatabase``, else the connection's ``current_schema()``.

    Only reads the catalog: the role needs no DDL privilege. A ``PostgresDatabase`` built with
    ``initialize_schema=False`` is not migrated by the check. Missing tables, columns, defaults,
    indexes and triggers, and type or NOT NULL differences that would break the Runtime, are
    returned as ``issues``; ``report.raise_for_issues()`` fails a test with all of them.
    """
    from algen_agent_runtime import __version__

    if schema is not None:
        check_schema_name(schema)
    if isinstance(target, str):
        dsn = target
        if dsn.startswith("env://"):
            variable = dsn.removeprefix("env://")
            dsn = os.environ.get(variable, "")
            if not dsn:
                raise ConfigurationError(f"environment variable {variable!r} is not set")
        try:
            import asyncpg
        except ImportError as exc:
            raise ImportError("install algen-agent-runtime[postgres]") from exc
        connection = await asyncpg.connect(dsn)
        try:
            return await _check(connection.fetch, schema, __version__)
        finally:
            await connection.close()
    if schema is None and isinstance(getattr(target, "schema", None), str):
        schema = target.schema
    return await _check(target.fetch, schema, __version__)


async def _check(fetch: Any, schema: str | None, version: str) -> SchemaReport:
    if schema is None:
        rows = await fetch("SELECT current_schema() AS name")
        schema = str(rows[0]["name"]) if rows and rows[0]["name"] is not None else "public"
    actual = await introspect(fetch, schema)
    issues = compare_catalogs(expected_catalog(), actual)
    names = [item.name for item in migrations()]
    unrecorded: list[str] = []
    if MIGRATIONS_TABLE in actual["tables"]:
        quoted = schema.replace('"', '""')
        recorded = {
            str(row["name"])
            for row in await fetch(f'SELECT name FROM "{quoted}".{MIGRATIONS_TABLE}')
        }
        unrecorded = [name for name in names if name not in recorded]
    else:
        unrecorded = names
    return SchemaReport(
        schema_name=schema,
        runtime_version=version,
        latest_migration=names[-1],
        issues=issues,
        unrecorded_migrations=unrecorded,
    )


def describe_migrations(items: Sequence[Migration] | None = None) -> list[dict[str, Any]]:
    """Each migration's number, name and first comment line, for ``migrations list --json``."""
    described = []
    for item in items if items is not None else migrations():
        summary = ""
        for line in item.sql.splitlines():
            if line.startswith("--"):
                summary = line.lstrip("- ").strip()
                break
        described.append({"number": item.number, "name": item.name, "summary": summary})
    return described

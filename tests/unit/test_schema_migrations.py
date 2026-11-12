"""The migrations command and check_schema's comparison, without a database."""

from __future__ import annotations

import copy
import json
import sys

import pytest

from algen_agent_runtime.cli import main
from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.persistence import check_schema, migration_sql, migrations
from algen_agent_runtime.persistence.schema import (
    MIGRATIONS_TABLE,
    compare_catalogs,
    expected_catalog,
)


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as exit_info:
        main(list(argv))
    captured = capsys.readouterr()
    return int(exit_info.value.code or 0), captured.out, captured.err


def test_migrations_are_numbered_in_order_without_gaps() -> None:
    numbers = [item.number for item in migrations()]
    assert numbers == list(range(1, len(numbers) + 1))
    assert migrations()[0].name == "001_initial.sql"


def test_the_catalog_was_regenerated_for_the_current_migrations() -> None:
    # After adding a migration: POSTGRES_DSN=... python scripts/schema_catalog.py --update
    assert expected_catalog()["migrations"] == [item.name for item in migrations()]


def test_migrations_list(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run_cli(capsys, "migrations", "list")
    assert code == 0
    lines = out.splitlines()
    assert [line.split()[0] for line in lines] == [item.name for item in migrations()]
    assert lines[19].startswith("020_approval_decided_by.sql  Who decided")

    code, out, _ = run_cli(capsys, "migrations", "list", "--json")
    described = json.loads(out)
    assert described[0] == {
        "number": 1,
        "name": "001_initial.sql",
        "summary": described[0]["summary"],
    }


def test_migrations_sql_prints_every_migration_in_order_with_its_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = run_cli(capsys, "migrations", "sql")
    assert code == 0
    assert out == migration_sql()
    positions = [out.index(f"-- algen-agent-runtime migration {m.name}") for m in migrations()]
    assert positions == sorted(positions)
    assert out.count(f"INSERT INTO {MIGRATIONS_TABLE}") == len(migrations())
    assert migrations()[0].sql.strip() in out
    assert not out.startswith(("CREATE SCHEMA", "SET search_path"))


def test_migrations_sql_since_and_schema(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run_cli(capsys, "migrations", "sql", "--since", "19", "--schema", "app_rt")
    assert code == 0
    assert out.startswith('CREATE SCHEMA IF NOT EXISTS "app_rt";\nSET search_path TO "app_rt";')
    assert "019_session_owners.sql" not in out
    assert "020_approval_decided_by.sql" in out
    assert "ADD COLUMN IF NOT EXISTS decided_by" in out

    code, out, _ = run_cli(capsys, "migrations", "sql", "--since", "20", "--no-record")
    assert MIGRATIONS_TABLE not in out
    assert "021_" in out and "020_" not in out


def test_migrations_sql_refuses_an_unknown_number_or_schema(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, err = run_cli(capsys, "migrations", "sql", "--since", "999")
    assert code == 1 and "no migration 999" in err
    code, _, err = run_cli(capsys, "migrations", "sql", "--schema", 'x"; DROP')
    assert code == 1 and "postgres_schema must be" in err


def test_the_latest_migration_prints_nothing_after_it(capsys: pytest.CaptureFixture[str]) -> None:
    latest = migrations()[-1].number
    code, out, _ = run_cli(capsys, "migrations", "sql", "--since", str(latest))
    assert code == 0 and out == ""


def test_compare_reports_what_is_missing_and_ignores_additions() -> None:
    expected = expected_catalog()
    assert compare_catalogs(expected, copy.deepcopy(expected)) == []

    actual = copy.deepcopy(expected)
    approvals = actual["tables"]["algen_agent_runtime_approvals"]
    del approvals["columns"]["decided_by"]
    approvals["columns"]["comment"]["type"] = "integer"
    approvals["columns"]["decided_at"]["not_null"] = True
    approvals["columns"]["host_column"] = {"type": "text", "not_null": False, "has_default": False}
    actual["tables"]["algen_agent_runtime_runs"]["columns"]["created_at"]["has_default"] = False
    actual["tables"]["algen_agent_runtime_runs"]["indexes"].remove(
        "algen_agent_runtime_runs_user_idx"
    )
    del actual["tables"]["algen_agent_runtime_session_owners"]
    del actual["tables"][MIGRATIONS_TABLE]
    actual["tables"]["host_table"] = {"columns": {}, "indexes": [], "triggers": []}

    found = {str(issue) for issue in compare_catalogs(expected, actual)}

    assert found == {
        "missing_column algen_agent_runtime_approvals.decided_by: expected text, found None",
        "type_mismatch algen_agent_runtime_approvals.comment: expected text, found integer",
        "not_null_mismatch algen_agent_runtime_approvals.decided_at: expected nullable, "
        "found NOT NULL",
        "missing_default algen_agent_runtime_runs.created_at: expected a default, found none",
        "missing_index algen_agent_runtime_runs (algen_agent_runtime_runs_user_idx)",
        "missing_table algen_agent_runtime_session_owners",
    }


async def test_check_schema_resolves_an_env_dsn_before_importing_asyncpg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALGEN_TEST_MISSING_DSN", raising=False)
    monkeypatch.setitem(sys.modules, "asyncpg", None)
    with pytest.raises(ConfigurationError, match="ALGEN_TEST_MISSING_DSN"):
        await check_schema("env://ALGEN_TEST_MISSING_DSN")
    monkeypatch.setenv("ALGEN_TEST_DSN", "postgresql://localhost/x")
    with pytest.raises(ImportError, match=r"algen-agent-runtime\[postgres\]"):
        await check_schema("env://ALGEN_TEST_DSN")


async def test_check_schema_reads_through_any_fetch() -> None:
    # A database with no Runtime table: every table is missing, every migration unrecorded.
    class Empty:
        schema = "app_rt"

        async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
            assert args == ("app_rt",)
            return []

    report = await check_schema(Empty())

    assert not report.ok
    assert report.schema_name == "app_rt"
    assert {issue.kind for issue in report.issues} == {"missing_table"}
    assert report.unrecorded_migrations == [item.name for item in migrations()]
    with pytest.raises(AssertionError, match="migrations sql --since"):
        report.raise_for_issues()

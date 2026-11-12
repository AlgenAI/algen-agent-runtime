"""Regenerate src/algen_agent_runtime/persistence/schema_catalog.json from the migrations.

``check_schema`` compares a live database with this catalog: every table, column (type,
NOT NULL, default), index and trigger the Runtime's migrations create. It is produced by
migrating a fresh scratch schema of a real PostgreSQL and reading its catalog, so it always
matches what the SQL does. Run it after adding a migration:

    POSTGRES_DSN=postgresql://... python scripts/schema_catalog.py --update

Without ``--update`` it prints the catalog. tests/unit/test_schema_migrations.py fails when the
catalog names a different set of migrations than the package has, and
tests/integration/test_schema_check_postgres.py when it differs from a migrated database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from typing import Any

from algen_agent_runtime.persistence.postgres import PostgresDatabase
from algen_agent_runtime.persistence.schema import CATALOG_PATH, introspect, migrations


async def build(dsn: str) -> dict[str, Any]:
    schema = f"algen_rt_catalog_{secrets.token_hex(4)}"
    database = PostgresDatabase(dsn, schema=schema)
    try:
        await database.start()
        catalog = await introspect(database.fetch, schema)
    finally:
        await database.close()
        import asyncpg

        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await connection.close()
    return {"migrations": [item.name for item in migrations()], **catalog}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="rewrite the packaged catalog")
    args = parser.parse_args()
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        print("set POSTGRES_DSN to a PostgreSQL the script may create a scratch schema in")
        return 2
    text = json.dumps(asyncio.run(build(dsn)), indent=2, sort_keys=True) + "\n"
    if args.update:
        CATALOG_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {CATALOG_PATH}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

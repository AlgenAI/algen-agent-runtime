"""Validation of PostgreSQL names the Runtime puts into SQL, kept free of store imports."""

from __future__ import annotations

import re

from algen_agent_runtime.exceptions.errors import ConfigurationError

_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def check_schema_name(schema: str) -> str:
    """``schema`` if it is a usable PostgreSQL schema name for the Runtime's tables.

    Only lowercase letters, digits and underscores, starting with a letter or underscore, at
    most 63 characters, and not a system schema: the name is put in ``search_path`` and in
    ``CREATE SCHEMA`` as a quoted identifier, so it must never need escaping.
    """
    if not _SCHEMA_NAME.fullmatch(schema) or schema.startswith("pg_"):
        raise ConfigurationError(
            "postgres_schema must be 1-63 lowercase letters, digits or underscores, start with a "
            "letter or underscore, and not start with pg_"
        )
    if schema == "information_schema":
        raise ConfigurationError("postgres_schema cannot be information_schema")
    return schema

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from algen_agent_runtime.auth.revocation import PostgresTokenRevocationStore
from algen_agent_runtime.persistence.postgres import PostgresDatabase

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("POSTGRES_DSN"), reason="token revocation requires a live PostgreSQL"
    ),
]


@pytest.fixture
async def database() -> AsyncIterator[PostgresDatabase]:
    pytest.importorskip("asyncpg")
    db = PostgresDatabase(os.environ["POSTGRES_DSN"])
    await db.start()
    try:
        yield db
    finally:
        await db.close()


async def test_postgres_token_revocation_round_trip(database: PostgresDatabase) -> None:
    store = PostgresTokenRevocationStore(database)
    tenant, other = f"tenant-{uuid4()}", f"tenant-{uuid4()}"
    now = datetime.now(UTC)
    await store.revoke_token(tenant, "jti-1", now + timedelta(minutes=5), reason="leak")
    assert (await store.state(tenant, "jti-1", "user-1")).token_revoked
    assert not (await store.state(other, "jti-1", "user-1")).token_revoked
    assert not (await store.state(tenant, None, "user-1")).token_revoked

    await store.revoke_subject(tenant, "user-1", now)
    await store.revoke_subject(tenant, "user-1", now - timedelta(days=1))
    cutoff = (await store.state(tenant, None, "user-1")).subject_revoked_before
    assert cutoff is not None and abs((cutoff - now).total_seconds()) < 1

    await store.revoke_token(tenant, "old", now - timedelta(seconds=1))
    assert not (await store.state(tenant, "old", "user-1")).token_revoked

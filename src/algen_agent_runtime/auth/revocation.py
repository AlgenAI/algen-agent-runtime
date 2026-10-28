"""Token revocation: a ``jti`` denylist and a per-subject "revoked before" timestamp.

Short-lived tokens are the primary control; this check is the optional fast path for a token
that must stop working before it expires. Entries are tenant-scoped, so one tenant's
administrator cannot revoke another tenant's tokens. ``jti`` entries are kept until the token's
own expiry and then purged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.types.contracts import utc_now


@dataclass(frozen=True)
class RevocationState:
    """What the store knows about one token: its jti is denied, and/or its subject's cutoff."""

    token_revoked: bool = False
    subject_revoked_before: datetime | None = None


@runtime_checkable
class TokenRevocationStore(Protocol):
    async def revoke_token(
        self, tenant_id: str, jti: str, expires_at: datetime, *, reason: str | None = None
    ) -> None:
        """Deny the token with this ``jti`` until ``expires_at`` (its own ``exp``)."""
        ...

    async def revoke_subject(
        self,
        tenant_id: str,
        subject: str,
        revoked_before: datetime,
        *,
        reason: str | None = None,
    ) -> None:
        """Deny every token for ``subject`` issued (``iat``) before ``revoked_before``."""
        ...

    async def state(self, tenant_id: str, jti: str | None, subject: str) -> RevocationState:
        """Revocation state for one verified token, in one lookup."""
        ...

    async def purge_expired(self, now: datetime | None = None) -> int:
        """Remove ``jti`` entries whose token has expired anyway; returns how many."""
        ...


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class InMemoryTokenRevocationStore:
    """Per-process store. Revocations are lost on restart and not shared between workers."""

    def __init__(self, max_entries: int = 100_000) -> None:
        self._tokens: dict[tuple[str, str], datetime] = {}
        self._subjects: dict[tuple[str, str], datetime] = {}
        self._max_entries = max_entries
        self._lock = asyncio.Lock()

    async def revoke_token(
        self, tenant_id: str, jti: str, expires_at: datetime, *, reason: str | None = None
    ) -> None:
        expires_at = _aware(expires_at)
        async with self._lock:
            now = utc_now()
            self._purge(now)
            if expires_at <= now:
                return
            key = (tenant_id, jti)
            if key not in self._tokens and len(self._tokens) >= self._max_entries:
                raise ConflictError("token revocation store is full")
            self._tokens[key] = max(expires_at, self._tokens.get(key, expires_at))

    async def revoke_subject(
        self,
        tenant_id: str,
        subject: str,
        revoked_before: datetime,
        *,
        reason: str | None = None,
    ) -> None:
        revoked_before = _aware(revoked_before)
        async with self._lock:
            key = (tenant_id, subject)
            if key not in self._subjects and len(self._subjects) >= self._max_entries:
                raise ConflictError("subject revocation store is full")
            current = self._subjects.get(key)
            # Monotonic: a later call can extend a cutoff but never silently un-revoke tokens.
            self._subjects[key] = (
                revoked_before if current is None else max(current, revoked_before)
            )

    async def state(self, tenant_id: str, jti: str | None, subject: str) -> RevocationState:
        async with self._lock:
            expires = self._tokens.get((tenant_id, jti)) if jti else None
            return RevocationState(
                token_revoked=expires is not None and expires > utc_now(),
                subject_revoked_before=self._subjects.get((tenant_id, subject)),
            )

    async def purge_expired(self, now: datetime | None = None) -> int:
        async with self._lock:
            return self._purge(now or utc_now())

    def _purge(self, now: datetime) -> int:
        expired = [key for key, expires in self._tokens.items() if expires <= now]
        for key in expired:
            del self._tokens[key]
        return len(expired)


class PostgresTokenRevocationStore:
    """Durable, shared store (migration ``013_token_revocation.sql``)."""

    def __init__(self, database: Any) -> None:
        self._database = database

    async def revoke_token(
        self, tenant_id: str, jti: str, expires_at: datetime, *, reason: str | None = None
    ) -> None:
        now = utc_now()
        expires_at = _aware(expires_at)
        await self.purge_expired(now)
        if expires_at <= now:
            return
        await self._database.execute(
            "INSERT INTO algen_agent_runtime_revoked_tokens "
            "(tenant_id, jti, expires_at, revoked_at, reason) VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT (tenant_id, jti) DO UPDATE SET expires_at=GREATEST("
            "algen_agent_runtime_revoked_tokens.expires_at, EXCLUDED.expires_at)",
            tenant_id,
            jti,
            expires_at,
            now,
            reason,
        )

    async def revoke_subject(
        self,
        tenant_id: str,
        subject: str,
        revoked_before: datetime,
        *,
        reason: str | None = None,
    ) -> None:
        await self._database.execute(
            "INSERT INTO algen_agent_runtime_revoked_subjects "
            "(tenant_id, subject, revoked_before, updated_at, reason) VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT (tenant_id, subject) DO UPDATE SET revoked_before=GREATEST("
            "algen_agent_runtime_revoked_subjects.revoked_before, EXCLUDED.revoked_before), "
            "updated_at=EXCLUDED.updated_at, reason=EXCLUDED.reason",
            tenant_id,
            subject,
            _aware(revoked_before),
            utc_now(),
            reason,
        )

    async def state(self, tenant_id: str, jti: str | None, subject: str) -> RevocationState:
        row = await self._database.fetchrow(
            "SELECT EXISTS (SELECT 1 FROM algen_agent_runtime_revoked_tokens "
            "WHERE tenant_id=$1 AND jti=$2 AND expires_at > $4) AS token_revoked, "
            "(SELECT revoked_before FROM algen_agent_runtime_revoked_subjects "
            "WHERE tenant_id=$1 AND subject=$3) AS subject_revoked_before",
            tenant_id,
            jti or "",
            subject,
            utc_now(),
        )
        if row is None:
            return RevocationState()
        return RevocationState(
            token_revoked=bool(row["token_revoked"]) and bool(jti),
            subject_revoked_before=row["subject_revoked_before"],
        )

    async def purge_expired(self, now: datetime | None = None) -> int:
        result = await self._database.execute(
            "DELETE FROM algen_agent_runtime_revoked_tokens WHERE expires_at <= $1",
            now or utc_now(),
        )
        try:
            return int(str(result).rsplit(" ", 1)[-1])
        except ValueError:
            return 0

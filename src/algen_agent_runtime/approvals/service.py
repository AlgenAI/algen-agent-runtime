from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.exceptions.errors import ConflictError, NotFoundError
from algen_agent_runtime.types.contracts import utc_now


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"
    EXPIRED = "expired"


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    step_id: str
    tenant_id: str
    proposed_action: str
    side_effect_summary: str
    redacted_parameters: dict[str, Any]
    risk: str
    expires_at: datetime
    status: ApprovalStatus = ApprovalStatus.PENDING
    modified_parameters: dict[str, Any] | None = None
    # The agent whose run asked, and when. None on approvals stored by earlier releases. The
    # PostgreSQL store keeps both in columns, not in the stored JSON (see STORED_JSON_EXCLUDE).
    agent: str | None = None
    created_at: datetime | None = None

    def effective(self, now: datetime) -> ApprovalRequest:
        """This request as a reader should see it: a pending request past ``expires_at`` is
        reported as expired, although its stored status changes only when it is decided."""
        if self.status == ApprovalStatus.PENDING and self.expires_at <= now:
            return self.model_copy(update={"status": ApprovalStatus.EXPIRED})
        return self


MAX_APPROVAL_PAGE = 500
MAX_APPROVAL_OFFSET = 1_000_000

# Fields the PostgreSQL store keeps in columns instead of the stored JSON. 0.1.0a17 validates the
# JSON with extra="forbid", so leaving them out keeps rows readable by it during a rolling deploy
# or after a rollback.
STORED_JSON_EXCLUDE = frozenset({"agent", "created_at"})


def _check_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= MAX_APPROVAL_PAGE:
        raise ValueError(f"limit must be between 1 and {MAX_APPROVAL_PAGE}")
    if not 0 <= offset <= MAX_APPROVAL_OFFSET:
        raise ValueError(f"offset must be between 0 and {MAX_APPROVAL_OFFSET}")


class ApprovalService(Protocol):
    async def create(
        self,
        run_id: str,
        step_id: str,
        tenant_id: str,
        proposed_action: str,
        parameters: dict[str, Any],
        risk: str,
        expires_seconds: int,
        *,
        agent: str | None = None,
    ) -> ApprovalRequest: ...

    async def decide(
        self,
        approval_id: str,
        tenant_id: str,
        decision: ApprovalStatus,
        modified_parameters: dict[str, Any] | None = None,
    ) -> ApprovalRequest: ...

    async def get(self, approval_id: str, tenant_id: str) -> ApprovalRequest | None: ...

    async def list(
        self,
        tenant_id: str,
        *,
        status: ApprovalStatus | None = None,
        run_id: str | None = None,
        agent: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[ApprovalRequest]:
        """The tenant's approval requests, newest first, each as
        :meth:`ApprovalRequest.effective` reports it.

        ``status``, ``run_id`` and ``agent`` narrow the list. ``status=PENDING`` leaves out
        requests past their expiry, and ``status=EXPIRED`` includes them. ``limit`` is 1 to
        ``MAX_APPROVAL_PAGE`` and ``offset`` 0 to ``MAX_APPROVAL_OFFSET``.

        Requests created at the same instant are ordered differently by each store: the
        in-memory store by insertion order, PostgreSQL by ``id`` (random). Both orders are
        stable between calls, which is all offset paging needs.
        """
        ...


class InMemoryApprovalService:
    def __init__(self) -> None:
        self._items: dict[str, ApprovalRequest] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        run_id: str,
        step_id: str,
        tenant_id: str,
        proposed_action: str,
        parameters: dict[str, Any],
        risk: str,
        expires_seconds: int,
        *,
        agent: str | None = None,
    ) -> ApprovalRequest:
        now = utc_now()
        request = ApprovalRequest(
            run_id=run_id,
            step_id=step_id,
            tenant_id=tenant_id,
            proposed_action=proposed_action,
            side_effect_summary=f"Proposed {risk} operation: {proposed_action}",
            redacted_parameters=parameters,
            risk=risk,
            expires_at=now + timedelta(seconds=expires_seconds),
            agent=agent,
            created_at=now,
        )
        async with self._lock:
            self._items[request.id] = request
        return request.model_copy(deep=True)

    async def decide(
        self,
        approval_id: str,
        tenant_id: str,
        decision: ApprovalStatus,
        modified_parameters: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        async with self._lock:
            item = self._items.get(approval_id)
            if item is None or item.tenant_id != tenant_id:
                raise NotFoundError("approval request not found")
            if item.status != ApprovalStatus.PENDING:
                raise ConflictError("approval request has already been decided")
            if item.expires_at <= utc_now():
                item.status = ApprovalStatus.EXPIRED
                raise ConflictError("approval request expired")
            if decision == ApprovalStatus.MODIFIED and modified_parameters is None:
                raise ValueError("modified decision requires modified_parameters")
            item.status = decision
            item.modified_parameters = modified_parameters
            return item.model_copy(deep=True)

    async def get(self, approval_id: str, tenant_id: str) -> ApprovalRequest | None:
        async with self._lock:
            item = self._items.get(approval_id)
            return item.model_copy(deep=True) if item and item.tenant_id == tenant_id else None

    async def list(
        self,
        tenant_id: str,
        *,
        status: ApprovalStatus | None = None,
        run_id: str | None = None,
        agent: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[ApprovalRequest]:
        _check_page(limit, offset)
        now = utc_now()
        async with self._lock:
            items = [
                item.effective(now).model_copy(deep=True)
                for item in self._items.values()
                if item.tenant_id == tenant_id
                and (run_id is None or item.run_id == run_id)
                and (agent is None or item.agent == agent)
            ]
        # Newest first; insertion order breaks ties between requests created together.
        ordered = [
            item
            for _, item in sorted(
                enumerate(items),
                key=lambda pair: (pair[1].created_at or now, pair[0]),
                reverse=True,
            )
            if status is None or item.status == status
        ]
        return tuple(ordered[offset : offset + limit])


class PostgresApprovalService:
    def __init__(self, database: Any) -> None:
        self._database = database

    async def create(
        self,
        run_id: str,
        step_id: str,
        tenant_id: str,
        proposed_action: str,
        parameters: dict[str, Any],
        risk: str,
        expires_seconds: int,
        *,
        agent: str | None = None,
    ) -> ApprovalRequest:
        now = utc_now()
        request = ApprovalRequest(
            run_id=run_id,
            step_id=step_id,
            tenant_id=tenant_id,
            proposed_action=proposed_action,
            side_effect_summary=f"Proposed {risk} operation: {proposed_action}",
            redacted_parameters=parameters,
            risk=risk,
            expires_at=now + timedelta(seconds=expires_seconds),
            agent=agent,
        )
        row = await self._database.fetchrow(
            "INSERT INTO algen_agent_runtime_approvals "
            "(id, run_id, tenant_id, agent, approval) VALUES ($1,$2,$3,$4,$5::jsonb) "
            "RETURNING created_at",
            request.id,
            run_id,
            tenant_id,
            agent,
            request.model_dump_json(exclude=set(STORED_JSON_EXCLUDE)),
        )
        # The row's time (the database clock), as get and list report it.
        return request.model_copy(update={"created_at": row["created_at"]})

    async def decide(
        self,
        approval_id: str,
        tenant_id: str,
        decision: ApprovalStatus,
        modified_parameters: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        item = await self.get(approval_id, tenant_id)
        if item is None:
            raise NotFoundError("approval request not found")
        if item.status != ApprovalStatus.PENDING:
            raise ConflictError("approval request has already been decided")
        if item.expires_at <= utc_now():
            updated = item.model_copy(update={"status": ApprovalStatus.EXPIRED})
            await self._save(updated, expected=ApprovalStatus.PENDING)
            raise ConflictError("approval request expired")
        if decision == ApprovalStatus.MODIFIED and modified_parameters is None:
            raise ValueError("modified decision requires modified_parameters")
        updated = item.model_copy(
            update={"status": decision, "modified_parameters": modified_parameters}
        )
        if not await self._save(updated, expected=ApprovalStatus.PENDING):
            raise ConflictError("approval request was concurrently decided")
        return updated

    async def get(self, approval_id: str, tenant_id: str) -> ApprovalRequest | None:
        row = await self._database.fetchrow(
            "SELECT approval, agent, created_at FROM algen_agent_runtime_approvals "
            "WHERE id=$1 AND tenant_id=$2",
            approval_id,
            tenant_id,
        )
        return _from_row(row) if row else None

    async def list(
        self,
        tenant_id: str,
        *,
        status: ApprovalStatus | None = None,
        run_id: str | None = None,
        agent: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[ApprovalRequest]:
        _check_page(limit, offset)
        now = utc_now()
        clauses = ["tenant_id=$1"]
        arguments: list[Any] = [tenant_id]

        def bind(value: Any) -> str:
            arguments.append(value)
            return f"${len(arguments)}"

        if run_id is not None:
            clauses.append(f"run_id={bind(run_id)}")
        if agent is not None:
            clauses.append(f"agent={bind(agent)}")
        if status is not None:
            stored = "approval->>'status'"
            if status in {ApprovalStatus.PENDING, ApprovalStatus.EXPIRED}:
                past_expiry = f"(approval->>'expires_at')::timestamptz <= {bind(now)}"
                clauses.append(
                    f"{stored}='pending' AND NOT {past_expiry}"
                    if status == ApprovalStatus.PENDING
                    else f"({stored}='expired' OR ({stored}='pending' AND {past_expiry}))"
                )
            else:
                clauses.append(f"{stored}={bind(status.value)}")
        rows = await self._database.fetch(
            "SELECT approval, agent, created_at FROM algen_agent_runtime_approvals "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at DESC, id DESC LIMIT {bind(limit)} OFFSET {bind(offset)}",
            *arguments,
        )
        return tuple(_from_row(row).effective(now) for row in rows)

    async def _save(self, item: ApprovalRequest, expected: ApprovalStatus) -> bool:
        result = await self._database.execute(
            "UPDATE algen_agent_runtime_approvals SET approval=$1::jsonb, updated_at=now() "
            "WHERE id=$2 AND tenant_id=$3 AND approval->>'status'=$4",
            item.model_dump_json(exclude=set(STORED_JSON_EXCLUDE)),
            item.id,
            item.tenant_id,
            expected.value,
        )
        return str(result) == "UPDATE 1"


def _from_row(row: Any) -> ApprovalRequest:
    """An approval row as a request: the stored JSON, with ``agent`` and ``created_at`` from
    their columns."""
    stored = row["approval"]
    # asyncpg returns jsonb as text.
    values = json.loads(stored) if isinstance(stored, (str, bytes)) else dict(stored)
    values.update(agent=row["agent"], created_at=row["created_at"])
    return ApprovalRequest.model_validate(values)

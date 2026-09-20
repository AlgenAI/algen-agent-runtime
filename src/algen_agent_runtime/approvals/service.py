from __future__ import annotations

import asyncio
from datetime import timedelta
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
    expires_at: Any
    status: ApprovalStatus = ApprovalStatus.PENDING
    modified_parameters: dict[str, Any] | None = None


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
    ) -> ApprovalRequest: ...

    async def decide(
        self,
        approval_id: str,
        tenant_id: str,
        decision: ApprovalStatus,
        modified_parameters: dict[str, Any] | None = None,
    ) -> ApprovalRequest: ...

    async def get(self, approval_id: str, tenant_id: str) -> ApprovalRequest | None: ...


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
    ) -> ApprovalRequest:
        request = ApprovalRequest(
            run_id=run_id,
            step_id=step_id,
            tenant_id=tenant_id,
            proposed_action=proposed_action,
            side_effect_summary=f"Proposed {risk} operation: {proposed_action}",
            redacted_parameters=parameters,
            risk=risk,
            expires_at=utc_now() + timedelta(seconds=expires_seconds),
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
    ) -> ApprovalRequest:
        request = ApprovalRequest(
            run_id=run_id,
            step_id=step_id,
            tenant_id=tenant_id,
            proposed_action=proposed_action,
            side_effect_summary=f"Proposed {risk} operation: {proposed_action}",
            redacted_parameters=parameters,
            risk=risk,
            expires_at=utc_now() + timedelta(seconds=expires_seconds),
        )
        await self._database.execute(
            "INSERT INTO algen_agent_runtime_approvals "
            "(id, run_id, tenant_id, approval) VALUES ($1,$2,$3,$4::jsonb)",
            request.id,
            run_id,
            tenant_id,
            request.model_dump_json(),
        )
        return request

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
            "SELECT approval FROM algen_agent_runtime_approvals WHERE id=$1 AND tenant_id=$2",
            approval_id,
            tenant_id,
        )
        return ApprovalRequest.model_validate_json(row["approval"]) if row else None

    async def _save(self, item: ApprovalRequest, expected: ApprovalStatus) -> bool:
        result = await self._database.execute(
            "UPDATE algen_agent_runtime_approvals SET approval=$1::jsonb, updated_at=now() "
            "WHERE id=$2 AND tenant_id=$3 AND approval->>'status'=$4",
            item.model_dump_json(),
            item.id,
            item.tenant_id,
            expected.value,
        )
        return str(result) == "UPDATE 1"

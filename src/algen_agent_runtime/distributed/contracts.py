from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.types.contracts import utc_now


class WorkStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str
    kind: str
    deduplication_key: str
    payload: dict[str, Any]
    status: WorkStatus = WorkStatus.PENDING
    attempts: int = 0
    maximum_attempts: int = Field(default=3, ge=1, le=20)
    available_at: datetime = Field(default_factory=utc_now)
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class WorkQueue(Protocol):
    async def enqueue(self, item: WorkItem) -> tuple[WorkItem, bool]: ...
    async def claim(self, worker_id: str, lease_seconds: int) -> WorkItem | None: ...
    async def renew(self, item_id: str, worker_id: str, lease_seconds: int) -> bool: ...
    async def complete(self, item_id: str, worker_id: str, result: dict[str, Any]) -> None: ...
    async def fail(self, item_id: str, worker_id: str, error: str, retry: bool) -> None: ...

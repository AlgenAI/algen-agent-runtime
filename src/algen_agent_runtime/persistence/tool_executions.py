from __future__ import annotations

import asyncio
from collections.abc import Collection, Sequence
from datetime import timedelta

import structlog

from algen_agent_runtime.tools.contracts import (
    ToolContext,
    ToolExecutionRecord,
    ToolExecutionStatus,
    ToolResult,
)
from algen_agent_runtime.types.contracts import utc_now

_logger = structlog.get_logger("algen_agent_runtime.tools.ledger")


class InMemoryToolExecutionStore:
    def __init__(self) -> None:
        self._records: dict[str, ToolExecutionRecord] = {}
        self._lock = asyncio.Lock()

    async def begin(self, context: ToolContext, tool_name: str) -> tuple[ToolExecutionRecord, bool]:
        async with self._lock:
            current = self._records.get(context.idempotency_key)
            if current and current.status not in {ToolExecutionStatus.FAILED}:
                return current.model_copy(deep=True), False
            now = utc_now()
            record = ToolExecutionRecord(
                idempotency_key=context.idempotency_key,
                run_id=context.run_id,
                step_id=context.step_id,
                tenant_id=context.tenant_id,
                tool_name=tool_name,
                status=ToolExecutionStatus.STARTED,
                created_at=current.created_at if current else now,
                updated_at=now,
                resolutions=current.resolutions if current else (),
            )
            self._records[context.idempotency_key] = record
            return record.model_copy(deep=True), True

    async def complete(self, idempotency_key: str, result: ToolResult) -> None:
        async with self._lock:
            current = self._records[idempotency_key]
            if not _still_started(current):
                return
            self._records[idempotency_key] = current.model_copy(
                update={
                    "status": ToolExecutionStatus.COMPLETED,
                    "result": result,
                    "error": None,
                    "updated_at": utc_now(),
                }
            )

    async def fail(self, idempotency_key: str, error: str, *, indeterminate: bool) -> None:
        async with self._lock:
            current = self._records[idempotency_key]
            if not _still_started(current):
                return
            status = (
                ToolExecutionStatus.INDETERMINATE if indeterminate else ToolExecutionStatus.FAILED
            )
            self._records[idempotency_key] = current.model_copy(
                update={"status": status, "error": error, "updated_at": utc_now()}
            )

    async def get(self, idempotency_key: str, tenant_id: str) -> ToolExecutionRecord | None:
        async with self._lock:
            current = self._records.get(idempotency_key)
            if current is None or current.tenant_id != tenant_id:
                return None
            return current.model_copy(deep=True)

    async def find(
        self,
        tenant_id: str,
        *,
        statuses: Collection[ToolExecutionStatus],
        run_id: str | None = None,
        tool_name: str | None = None,
        min_age: timedelta | None = None,
        limit: int = 100,
    ) -> Sequence[ToolExecutionRecord]:
        wanted = frozenset(statuses)
        cutoff = utc_now() - min_age if min_age is not None else None
        async with self._lock:
            matches = [
                record.model_copy(deep=True)
                for record in self._records.values()
                if record.tenant_id == tenant_id
                and record.status in wanted
                and (run_id is None or record.run_id == run_id)
                and (tool_name is None or record.tool_name == tool_name)
                and (cutoff is None or (record.updated_at or utc_now()) <= cutoff)
            ]
        matches.sort(key=lambda record: record.updated_at or utc_now())
        return tuple(matches[:limit])

    async def transition(
        self, record: ToolExecutionRecord, *, expected: Collection[ToolExecutionStatus]
    ) -> ToolExecutionRecord | None:
        async with self._lock:
            current = self._records.get(record.idempotency_key)
            if (
                current is None
                or current.tenant_id != record.tenant_id
                or current.status not in frozenset(expected)
            ):
                return None
            stored = record.model_copy(
                update={"created_at": current.created_at, "updated_at": utc_now()}
            )
            self._records[record.idempotency_key] = stored
            return stored.model_copy(deep=True)


def _still_started(record: ToolExecutionRecord) -> bool:
    """Whether a call's own outcome may still be written: an operator has not settled it."""
    if record.status == ToolExecutionStatus.STARTED:
        return True
    # The call outlived an operator's decision; that decision stands.
    _logger.warning(
        "tool_execution_late_outcome_ignored",
        idempotency_key=record.idempotency_key,
        tool=record.tool_name,
        status=record.status.value,
    )
    return False

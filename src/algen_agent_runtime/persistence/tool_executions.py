from __future__ import annotations

import asyncio

from algen_agent_runtime.tools.contracts import (
    ToolContext,
    ToolExecutionRecord,
    ToolExecutionStatus,
    ToolResult,
)


class InMemoryToolExecutionStore:
    def __init__(self) -> None:
        self._records: dict[str, ToolExecutionRecord] = {}
        self._lock = asyncio.Lock()

    async def begin(self, context: ToolContext, tool_name: str) -> tuple[ToolExecutionRecord, bool]:
        async with self._lock:
            current = self._records.get(context.idempotency_key)
            if current and current.status not in {ToolExecutionStatus.FAILED}:
                return current.model_copy(deep=True), False
            record = ToolExecutionRecord(
                idempotency_key=context.idempotency_key,
                run_id=context.run_id,
                step_id=context.step_id,
                tenant_id=context.tenant_id,
                tool_name=tool_name,
                status=ToolExecutionStatus.STARTED,
            )
            self._records[context.idempotency_key] = record
            return record.model_copy(deep=True), True

    async def complete(self, idempotency_key: str, result: ToolResult) -> None:
        async with self._lock:
            current = self._records[idempotency_key]
            self._records[idempotency_key] = current.model_copy(
                update={"status": ToolExecutionStatus.COMPLETED, "result": result, "error": None}
            )

    async def fail(self, idempotency_key: str, error: str, *, indeterminate: bool) -> None:
        async with self._lock:
            current = self._records[idempotency_key]
            status = (
                ToolExecutionStatus.INDETERMINATE if indeterminate else ToolExecutionStatus.FAILED
            )
            self._records[idempotency_key] = current.model_copy(
                update={"status": status, "error": error}
            )

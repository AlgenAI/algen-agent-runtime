from datetime import timedelta
from typing import Any

import pytest

from algen_agent_runtime.approvals.service import (
    ApprovalRequest,
    ApprovalStatus,
    PostgresApprovalService,
)
from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.types.contracts import utc_now


class _ApprovalDatabase:
    """Stores the approval as JSON text, as asyncpg returns jsonb columns."""

    def __init__(self, approval: ApprovalRequest) -> None:
        self.approval_json = approval.model_dump_json()

    async def fetchrow(self, query: str, *args: Any) -> dict[str, str]:
        return {"approval": self.approval_json}

    async def execute(self, query: str, *args: Any) -> str:
        self.approval_json = args[0]
        return "UPDATE 1"


def _request(expires_in: timedelta) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="run-1",
        step_id="step-1",
        tenant_id="tenant-1",
        proposed_action="Continue",
        side_effect_summary="Continue the task",
        redacted_parameters={},
        risk="bounded",
        expires_at=utc_now() + expires_in,
    )


async def test_postgres_approval_decision_after_json_round_trip() -> None:
    request = _request(timedelta(minutes=5))
    service = PostgresApprovalService(_ApprovalDatabase(request))

    decided = await service.decide(request.id, request.tenant_id, ApprovalStatus.APPROVED)

    assert decided.status == ApprovalStatus.APPROVED
    assert decided.expires_at.tzinfo is not None


async def test_postgres_approval_expiry_is_enforced_after_json_round_trip() -> None:
    request = _request(timedelta(seconds=-1))
    database = _ApprovalDatabase(request)
    service = PostgresApprovalService(database)

    with pytest.raises(ConflictError, match="expired"):
        await service.decide(request.id, request.tenant_id, ApprovalStatus.APPROVED)

    assert ApprovalRequest.model_validate_json(database.approval_json).status == (
        ApprovalStatus.EXPIRED
    )

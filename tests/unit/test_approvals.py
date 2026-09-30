from datetime import timedelta

from algen_agent_runtime.approvals.service import (
    ApprovalRequest,
    ApprovalStatus,
    PostgresApprovalService,
)
from algen_agent_runtime.types.contracts import utc_now


class _ApprovalDatabase:
    def __init__(self, approval: ApprovalRequest) -> None:
        self.approval = approval

    async def fetchrow(self, query: str, *args):
        return {"approval": self.approval.model_dump_json()}

    async def execute(self, query: str, *args):
        self.approval = ApprovalRequest.model_validate_json(args[0])
        return "UPDATE 1"


async def test_postgres_approval_expiry_round_trip_remains_a_datetime() -> None:
    request = ApprovalRequest(
        run_id="run-1",
        step_id="step-1",
        tenant_id="tenant-1",
        proposed_action="Continue",
        side_effect_summary="Continue the task",
        redacted_parameters={},
        risk="bounded",
        expires_at=utc_now() + timedelta(minutes=5),
    )
    database = _ApprovalDatabase(request)
    service = PostgresApprovalService(database)

    decided = await service.decide(
        request.id, request.tenant_id, ApprovalStatus.APPROVED
    )

    assert decided.status == ApprovalStatus.APPROVED
    assert decided.expires_at.tzinfo is not None

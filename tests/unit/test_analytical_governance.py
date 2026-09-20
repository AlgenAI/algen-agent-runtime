from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from algen_agent_runtime.exceptions.errors import PolicyDeniedError
from algen_agent_runtime.governance import (
    DataTrustRequirement,
    GovernedQueryExecutor,
    GovernedQueryRequest,
    QueryEstimate,
    QueryGovernanceEngine,
    QueryGovernancePolicy,
    query_fingerprint,
)


def test_query_governance_enforces_trust_auth_purpose_and_quota() -> None:
    engine = QueryGovernanceEngine(
        QueryGovernancePolicy(
            allowed_purposes=("executive_analytics",),
            required_authorization_tags=("revenue.read",),
            maximum_rows=100,
        )
    )
    request = GovernedQueryRequest(
        tenant_id="t1",
        user_id="u1",
        purpose="executive_analytics",
        source_ids=("sales",),
        authorization_tags=("revenue.read",),
        trust=(
            DataTrustRequirement(
                source_id="sales",
                observed_at=datetime.now(UTC) - timedelta(minutes=1),
                maximum_age_seconds=300,
                completeness=0.99,
                minimum_completeness=0.95,
                certified=True,
            ),
        ),
        estimate=QueryEstimate(estimated_rows=50),
    )
    assert engine.enforce(request).decision == "allow"
    with pytest.raises(PolicyDeniedError, match="row_quota_exceeded"):
        engine.enforce(request.model_copy(update={"estimate": QueryEstimate(estimated_rows=101)}))


def test_query_fingerprint_includes_authorization_boundary() -> None:
    first = query_fingerprint("SELECT 1", (), authorization_fingerprint="role-a")
    second = query_fingerprint("SELECT 1", (), authorization_fingerprint="role-b")
    assert first != second


def test_certified_source_policy_fails_closed_when_trust_evidence_is_missing() -> None:
    engine = QueryGovernanceEngine(QueryGovernancePolicy(require_certified_sources=True))
    request = GovernedQueryRequest(
        tenant_id="t1",
        user_id="u1",
        purpose="analytics",
        source_ids=("sales",),
    )
    with pytest.raises(PolicyDeniedError, match="source_trust_missing:sales"):
        engine.enforce(request)


def test_freshness_requirement_fails_closed_without_observation_time() -> None:
    engine = QueryGovernanceEngine(QueryGovernancePolicy(require_certified_sources=True))
    request = GovernedQueryRequest(
        tenant_id="t1",
        user_id="u1",
        purpose="analytics",
        source_ids=("sales",),
        trust=(
            DataTrustRequirement(
                source_id="sales",
                certified=True,
                maximum_age_seconds=300,
            ),
        ),
    )
    with pytest.raises(PolicyDeniedError, match="source_freshness_unknown:sales"):
        engine.enforce(request)


async def test_governed_executor_checks_actual_row_count_after_execution() -> None:
    class Backend:
        async def execute(self, sql, parameters):
            del sql, parameters
            return [{"value": 1}, {"value": 2}]

    governance = QueryGovernanceEngine(
        QueryGovernancePolicy(require_certified_sources=True, maximum_rows=1)
    )
    executor = GovernedQueryExecutor(Backend(), governance)
    request = GovernedQueryRequest(
        tenant_id="t1",
        user_id="u1",
        purpose="analytics",
        source_ids=("sales",),
        trust=(DataTrustRequirement(source_id="sales", certified=True),),
        estimate=QueryEstimate(estimated_rows=1),
    )
    with pytest.raises(PolicyDeniedError, match="actual row quota"):
        await executor.execute("SELECT value FROM sales", (), request)

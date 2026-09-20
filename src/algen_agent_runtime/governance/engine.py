from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from opentelemetry import metrics, trace

from algen_agent_runtime.exceptions.errors import PolicyDeniedError
from algen_agent_runtime.governance.contracts import (
    ExportAction,
    GovernanceDecision,
    GovernanceDecisionType,
    GovernedQueryRequest,
    QueryGovernancePolicy,
)


class QueryGovernanceEngine:
    """Deterministic data-trust, authorization, quota, and export-policy gate."""

    def __init__(self, policy: QueryGovernancePolicy) -> None:
        self.policy = policy
        self._tracer = trace.get_tracer("algen_agent_runtime.governance")
        self._counter = metrics.get_meter("algen_agent_runtime.governance").create_counter(
            "algen_agent_runtime.governance.decisions"
        )

    def evaluate(self, request: GovernedQueryRequest) -> GovernanceDecision:
        reasons: list[str] = []
        if self.policy.allowed_purposes and request.purpose not in self.policy.allowed_purposes:
            reasons.append("purpose_not_allowed")
        missing_tags = set(self.policy.required_authorization_tags) - set(
            request.authorization_tags
        )
        if missing_tags:
            reasons.append("missing_authorization_tags")
        if set(request.columns) & set(self.policy.denied_columns):
            reasons.append("denied_column")
        if self.policy.allowed_metrics and not set(request.metrics).issubset(
            self.policy.allowed_metrics
        ):
            reasons.append("metric_not_allowed")
        now = datetime.now(UTC)
        trust_by_source = {item.source_id: item for item in request.trust}
        if self.policy.require_certified_sources:
            for source_id in set(request.source_ids) - set(trust_by_source):
                reasons.append(f"source_trust_missing:{source_id}")
        for trust in request.trust:
            if self.policy.require_certified_sources and not trust.certified:
                reasons.append(f"source_not_certified:{trust.source_id}")
            if trust.maximum_age_seconds is not None and trust.observed_at is None:
                reasons.append(f"source_freshness_unknown:{trust.source_id}")
            if (
                trust.maximum_age_seconds is not None
                and trust.observed_at is not None
                and (now - trust.observed_at).total_seconds() > trust.maximum_age_seconds
            ):
                reasons.append(f"source_stale:{trust.source_id}")
            if trust.minimum_completeness is not None and (
                trust.completeness is None or trust.completeness < trust.minimum_completeness
            ):
                reasons.append(f"source_incomplete:{trust.source_id}")
        estimate = request.estimate
        if estimate.estimated_rows > self.policy.maximum_rows:
            reasons.append("row_quota_exceeded")
        if estimate.estimated_bytes_scanned > self.policy.maximum_bytes_scanned:
            reasons.append("scan_quota_exceeded")
        if estimate.estimated_compute_seconds > self.policy.maximum_compute_seconds:
            reasons.append("compute_quota_exceeded")
        action_policy = {
            ExportAction.VIEW_SQL: self.policy.allow_sql_visibility,
            ExportAction.DOWNLOAD: self.policy.allow_download,
            ExportAction.EXPORT_ARTIFACT: self.policy.allow_artifact_export,
        }
        if any(not action_policy[action] for action in request.requested_actions):
            reasons.append("export_action_not_allowed")
        allowed = not reasons
        decision = GovernanceDecision(
            decision=GovernanceDecisionType.ALLOW if allowed else GovernanceDecisionType.DENY,
            reason_code="all_checks_passed" if allowed else reasons[0],
            reasons=tuple(reasons),
            audit_metadata={
                "tenant_id": request.tenant_id,
                "purpose": request.purpose,
                "source_count": len(request.source_ids),
                "query_fingerprint": request.query_fingerprint,
                "audit_retention_days": self.policy.audit_retention_days,
            },
        )
        attributes = {
            "governance.decision": decision.decision.value,
            "governance.reason_code": decision.reason_code,
            "governance.purpose": request.purpose,
        }
        with self._tracer.start_as_current_span("query_governance.evaluate", attributes=attributes):
            self._counter.add(1, attributes)
        return decision

    def enforce(self, request: GovernedQueryRequest) -> GovernanceDecision:
        decision = self.evaluate(request)
        if decision.decision == GovernanceDecisionType.DENY:
            raise PolicyDeniedError(
                f"governed query denied ({decision.reason_code}): {', '.join(decision.reasons)}"
            )
        return decision


def query_fingerprint(
    sql: str,
    parameters: tuple[object, ...],
    *,
    semantic_digest: str | None = None,
    authorization_fingerprint: str | None = None,
) -> str:
    material = json.dumps(
        {
            "sql": " ".join(sql.split()),
            "parameters": parameters,
            "semantic_digest": semantic_digest,
            "authorization_fingerprint": authorization_fingerprint,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode()).hexdigest()

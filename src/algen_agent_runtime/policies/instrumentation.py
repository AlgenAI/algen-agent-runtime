from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from algen_agent_runtime.policies.contracts import PolicyAction, PolicyDecision, PolicyPoint


async def evaluate_policy_observed(
    *,
    tracer: Any,
    engine: Any,
    point: str | PolicyPoint,
    payload: Any,
    context: Mapping[str, Any],
    attributes: Mapping[str, Any] | None = None,
) -> PolicyDecision:
    """Evaluate one policy boundary using the runtime's stable telemetry contract."""

    boundary = PolicyPoint(point).value
    span_attributes = dict(attributes or {})
    span_attributes.update(
        {
            "span.type": "policy_pipeline",
            "policy.operation": "agent.policy.evaluate",
            "policy.boundary": boundary,
            "policy.subject_type": type(payload).__name__,
            "policy.source_sdk": "algen_agent_runtime",
            "governance.event_type": "policy_decision",
        }
    )
    with tracer.start_as_current_span(
        f"agent.policy.{boundary}", attributes=span_attributes
    ) as span:
        raw_decision = await engine.evaluate(boundary, payload, context)
        if isinstance(raw_decision, PolicyDecision):
            decision = raw_decision
        elif isinstance(raw_decision, Mapping):
            decision = PolicyDecision.model_validate(raw_decision)
        else:
            # Keep simple decision-like test doubles and legacy policy engines
            # usable while normalizing the boundary to the typed contract.
            decision = PolicyDecision(
                action=raw_decision.action,
                reason_code=getattr(raw_decision, "reason_code", "policy.unspecified"),
                reason=getattr(raw_decision, "reason", "Policy decision returned."),
                value=getattr(raw_decision, "value", None),
                audit_metadata=getattr(raw_decision, "audit_metadata", {}),
            )
        invoked = tuple(decision.audit_metadata.get("policies_invoked", ()))
        triggered = tuple(decision.audit_metadata.get("policies_triggered", ()))
        skipped = tuple(decision.audit_metadata.get("policies_skipped", ()))
        span.set_attribute("policy.action", decision.action.value)
        span.set_attribute("policy.reason_code", decision.reason_code)
        span.set_attribute("policy.invoked_count", len(invoked))
        span.set_attribute("policy.triggered_count", len(triggered))
        span.set_attribute("policy.skipped_count", len(skipped))
        span.set_attribute("policy.triggered", decision.action != PolicyAction.ALLOW)
        if invoked:
            span.set_attribute("policy.policies_invoked", tuple(str(item) for item in invoked))
        if triggered:
            span.set_attribute("policy.policies_triggered", tuple(str(item) for item in triggered))
        if skipped:
            span.set_attribute("policy.policies_skipped", tuple(str(item) for item in skipped))
        return decision

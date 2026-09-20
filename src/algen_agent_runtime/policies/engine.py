from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any, Protocol

from opentelemetry import trace
from pydantic import BaseModel

from algen_agent_runtime.policies.contracts import (
    PolicyAction,
    PolicyDecision,
    PolicyPoint,
)
from algen_agent_runtime.tools.contracts import SideEffect

_CONTENT_BOUNDARIES = frozenset(PolicyPoint) - {PolicyPoint.AFTER_MEMORY_WRITE}


class Policy(Protocol):
    name: str
    category: str
    enforcement_mode: str
    applies_to: frozenset[PolicyPoint]

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision: ...


class SecretRedactionPolicy:
    name = "secrets"
    category = "input_validation"
    enforcement_mode = "warn"
    applies_to = _CONTENT_BOUNDARIES
    _pattern = re.compile(
        r"(?i)(?:api[_-]?key|password|secret|token)\s*[:=]\s*([A-Za-z0-9_\-/.]{8,})"
    )

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        transformed, changed = self._redact(payload)
        if changed:
            return PolicyDecision(
                action=PolicyAction.TRANSFORM,
                reason_code="secret.redacted",
                reason="Potential secret was redacted.",
                value=transformed,
            )
        return PolicyDecision(
            action=PolicyAction.ALLOW, reason_code="secret.clear", reason="No secret detected."
        )

    def _redact(self, value: Any) -> tuple[Any, bool]:
        if isinstance(value, str):
            transformed, count = self._pattern.subn(
                lambda match: match.group(0).replace(match.group(1), "[REDACTED]"),
                value,
            )
            return transformed, bool(count)
        if isinstance(value, BaseModel):
            updates: dict[str, Any] = {}
            changed = False
            for name in type(value).model_fields:
                item, item_changed = self._redact(getattr(value, name))
                updates[name] = item
                changed = changed or item_changed
            return value.model_copy(update=updates) if changed else value, changed
        if isinstance(value, Mapping):
            transformed_mapping: dict[Any, Any] = {}
            changed = False
            for key, item in value.items():
                transformed_item, item_changed = self._redact(item)
                transformed_mapping[key] = transformed_item
                changed = changed or item_changed
            return transformed_mapping if changed else value, changed
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            transformed_items = []
            changed = False
            for item in value:
                transformed_item, item_changed = self._redact(item)
                transformed_items.append(transformed_item)
                changed = changed or item_changed
            if not changed:
                return value, False
            return tuple(transformed_items) if isinstance(value, tuple) else transformed_items, True
        return value, False


class PIIRedactionPolicy:
    """Redact common direct identifiers before they cross an agent boundary.

    The patterns are deliberately conservative. Deployments can register a specialised
    DLP policy for jurisdiction- or institution-specific identifiers.
    """

    name = "pii"
    category = "pii"
    enforcement_mode = "warn"
    applies_to = _CONTENT_BOUNDARIES
    _patterns = (
        ("email", re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)),
        (
            "phone",
            re.compile(r"(?<!\d)(?:\+?91[\s.-]?)?[6-9]\d{4}[\s.-]?\d{5}(?!\d)"),
        ),
        (
            "aadhaar",
            re.compile(r"(?<!\d)[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}(?!\d)"),
        ),
    )

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        transformed, labels = self._redact(payload)
        if labels:
            return PolicyDecision(
                action=PolicyAction.REDACT,
                reason_code="pii.redacted",
                reason="Direct personal identifiers were redacted.",
                value=transformed,
                audit_metadata={"categories": sorted(labels), "count": len(labels)},
            )
        return PolicyDecision(
            action=PolicyAction.ALLOW, reason_code="pii.clear", reason="No PII detected."
        )

    def _redact(self, value: Any) -> tuple[Any, set[str]]:
        if isinstance(value, str):
            transformed = value
            labels: set[str] = set()
            for label, pattern in self._patterns:
                transformed, count = pattern.subn(f"[REDACTED_{label.upper()}]", transformed)
                if count:
                    labels.add(label)
            return transformed, labels
        if isinstance(value, BaseModel):
            updates: dict[str, Any] = {}
            model_labels: set[str] = set()
            for name in type(value).model_fields:
                item, item_labels = self._redact(getattr(value, name))
                updates[name] = item
                model_labels.update(item_labels)
            return value.model_copy(update=updates) if model_labels else value, model_labels
        if isinstance(value, Mapping):
            transformed_mapping: dict[Any, Any] = {}
            mapping_labels: set[str] = set()
            for key, item in value.items():
                transformed_item, item_labels = self._redact(item)
                transformed_mapping[key] = transformed_item
                mapping_labels.update(item_labels)
            return transformed_mapping if mapping_labels else value, mapping_labels
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            transformed_items = []
            sequence_labels: set[str] = set()
            for item in value:
                transformed_item, item_labels = self._redact(item)
                transformed_items.append(transformed_item)
                sequence_labels.update(item_labels)
            if not sequence_labels:
                return value, sequence_labels
            transformed_sequence = (
                tuple(transformed_items) if isinstance(value, tuple) else transformed_items
            )
            return transformed_sequence, sequence_labels
        return value, set()


class AuthorizationPolicy:
    name = "authorization"
    category = "tool_permission"
    enforcement_mode = "block"
    applies_to = frozenset({PolicyPoint.BEFORE_TOOL})

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        if point == "before_tool":
            definition = context["tool"]
            tool_context = context["context"]
            if not definition.required_permissions.issubset(tool_context.permissions):
                return PolicyDecision(
                    action=PolicyAction.DENY,
                    reason_code="auth.missing_permission",
                    reason="Tool permission check failed.",
                )
        return PolicyDecision(
            action=PolicyAction.ALLOW, reason_code="auth.allowed", reason="Authorized."
        )


class SideEffectPolicy:
    name = "side_effects"
    category = "tool_permission"
    enforcement_mode = "block"
    applies_to = frozenset({PolicyPoint.PLAN_TOOL})

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        definition = context.get("tool")
        approval_policy = context.get("approval_policy")
        if point == "plan_tool" and definition and approval_policy:
            side_effect = definition.side_effect
            required = (
                approval_policy.require_for_destructive and side_effect == SideEffect.DESTRUCTIVE
            ) or (
                approval_policy.require_for_side_effects
                and side_effect in {SideEffect.WRITE, SideEffect.EXTERNAL, SideEffect.DESTRUCTIVE}
            )
            if required:
                return PolicyDecision(
                    action=PolicyAction.REQUIRE_APPROVAL,
                    reason_code="side_effect.approval_required",
                    reason=f"Tool {definition.name} has {side_effect.value} side effects.",
                    audit_metadata={"risk": side_effect.value},
                )
        return PolicyDecision(
            action=PolicyAction.ALLOW, reason_code="side_effect.allowed", reason="Allowed."
        )


class PromptInjectionPolicy:
    """Block clear attempts to override or disclose privileged instructions.

    This intentionally targets high-signal imperative attacks. Applications can
    replace it with a specialised classifier without changing orchestration.
    """

    name = "prompt_injection"
    category = "prompt_injection"
    enforcement_mode = "block"
    applies_to = frozenset({PolicyPoint.INPUT})
    _patterns = (
        re.compile(
            r"(?i)\bignore\s+(?:all\s+)?(?:previous|prior|system|developer)\s+instructions?\b"
        ),
        re.compile(
            r"(?i)\b(?:reveal|print|show|repeat|expose)\b.{0,40}\b(?:system|developer)\s+(?:prompt|message|instructions?)\b"
        ),
        re.compile(
            r"(?i)\b(?:bypass|disable|override)\b.{0,40}\b(?:guardrails?|safety|policy|instructions?)\b"
        ),
        re.compile(r"(?i)\b(?:jailbreak|do anything now|DAN mode)\b"),
    )

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        del context
        if point != "input" or not isinstance(payload, str):
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                reason_code="prompt_injection.not_applicable",
                reason="Prompt-injection scan is not applicable at this boundary.",
            )
        if any(pattern.search(payload) for pattern in self._patterns):
            return PolicyDecision(
                action=PolicyAction.DENY,
                reason_code="prompt_injection.detected",
                reason="The request contains an attempt to override privileged instructions.",
                audit_metadata={"risk": "prompt_injection", "intervention": "blocked"},
            )
        return PolicyDecision(
            action=PolicyAction.ALLOW,
            reason_code="prompt_injection.clear",
            reason="No high-confidence prompt-injection pattern detected.",
        )


class OutputValidationPolicy:
    """Apply deterministic safety checks before model output reaches a user."""

    name = "output_validation"
    category = "output_validation"
    enforcement_mode = "block"
    applies_to = frozenset({PolicyPoint.AFTER_MODEL, PolicyPoint.FINAL_RESPONSE})
    _privileged_marker = re.compile(
        r"(?i)(?:^|\n)\s*(?:system|developer)\s+(?:prompt|message|instructions?)\s*:"
    )

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        if point not in {"after_model", "final_response"}:
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                reason_code="output_validation.not_applicable",
                reason="Output validation is not applicable at this boundary.",
            )
        if (not isinstance(payload, str) or not payload.strip()) and context.get("tool_calls"):
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                reason_code="output_validation.tool_calls",
                reason="An empty assistant message is valid while requesting tools.",
            )
        if not isinstance(payload, str) or not payload.strip():
            return PolicyDecision(
                action=PolicyAction.DENY,
                reason_code="output_validation.empty",
                reason="The generated response was empty.",
            )
        if self._privileged_marker.search(payload):
            return PolicyDecision(
                action=PolicyAction.DENY,
                reason_code="output_validation.privileged_instructions",
                reason="The response appears to disclose privileged instructions.",
                audit_metadata={"risk": "instruction_disclosure", "intervention": "blocked"},
            )
        return PolicyDecision(
            action=PolicyAction.ALLOW,
            reason_code="output_validation.passed",
            reason="The response passed deterministic output checks.",
        )


class CompositePolicyEngine:
    def __init__(
        self,
        policies: Sequence[Policy] | None = None,
        guardrail_scope: Callable[..., AbstractContextManager[Any]] | None = None,
    ) -> None:
        configured = policies or (
            SecretRedactionPolicy(),
            PIIRedactionPolicy(),
            PromptInjectionPolicy(),
            OutputValidationPolicy(),
            AuthorizationPolicy(),
            SideEffectPolicy(),
        )
        self._policies: dict[str, Policy] = {policy.name: policy for policy in configured}
        self._tracer = trace.get_tracer("algen_agent_runtime.guardrails")
        self._guardrail_scope = guardrail_scope or self._otel_guardrail_scope

    def _otel_guardrail_scope(
        self,
        *,
        name: str,
        category: str,
        enforcement_mode: str,
        policy_id: str | None = None,
    ) -> AbstractContextManager[Any]:
        attributes: dict[str, Any] = {
            "span.type": "guardrail",
            "guardrail.name": name,
            "guardrail.category": category,
            "guardrail.enforcement_mode": enforcement_mode,
            "guardrail.source_sdk": "algen_agent_runtime",
            "guardrail.evidence_type": "span_attribute",
        }
        if policy_id:
            attributes["guardrail.policy_id"] = policy_id
        return self._tracer.start_as_current_span(f"guardrail.{name}", attributes=attributes)

    def register(self, policy: Policy, *, replace: bool = False) -> None:
        if policy.name in self._policies and not replace:
            raise ValueError(f"policy {policy.name!r} is already registered")
        self._policies[policy.name] = policy

    def list(self) -> tuple[str, ...]:
        return tuple(self._policies)

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        try:
            policy_point = PolicyPoint(point)
        except ValueError as exc:
            expected = ", ".join(item.value for item in PolicyPoint)
            raise ValueError(
                f"unknown policy point {point!r}; expected one of: {expected}"
            ) from exc
        transformed = payload
        transforms: list[str] = []
        agent = context.get("agent")
        selected = set(getattr(getattr(agent, "guardrail_policy", None), "policies", ()))
        # Authorization and side-effect approval are runtime security invariants.
        selected.update({"authorization", "side_effects"})
        invoked: list[str] = []
        triggered: list[str] = []
        skipped: list[str] = []
        for policy in self._policies.values():
            if agent is not None and policy.name not in selected:
                continue
            applies_to = getattr(policy, "applies_to", None)
            # Policies registered by older plugins remain compatible and apply at
            # every boundary until they declare a narrower applicability contract.
            if applies_to is not None and policy_point not in applies_to:
                skipped.append(policy.name)
                continue
            invoked.append(policy.name)
            with self._guardrail_scope(
                name=policy.name,
                category=policy.category,
                enforcement_mode=getattr(policy, "enforcement_mode", "unknown"),
            ) as guardrail_span:
                guardrail_span.set_attribute("guardrail.boundary", point)
                decision = await policy.evaluate(point, transformed, context)
                triggered_now = decision.action != PolicyAction.ALLOW
                guardrail_span.set_attribute("guardrail.triggered", triggered_now)
                guardrail_span.set_attribute("guardrail.reason_code", decision.reason_code)
            if decision.action in {
                PolicyAction.DENY,
                PolicyAction.REQUIRE_CLARIFICATION,
                PolicyAction.REQUIRE_APPROVAL,
            }:
                return decision.model_copy(
                    update={
                        "audit_metadata": {
                            **decision.audit_metadata,
                            "policies_invoked": invoked,
                            "policies_triggered": [*triggered, policy.name],
                            "policies_skipped": skipped,
                        }
                    }
                )
            if decision.action in {PolicyAction.REDACT, PolicyAction.TRANSFORM}:
                transformed = decision.value
                transforms.append(decision.reason_code)
                triggered.append(policy.name)
        if transforms:
            return PolicyDecision(
                action=PolicyAction.TRANSFORM,
                reason_code="policy.transformed",
                reason="Payload transformed by policy.",
                value=transformed,
                audit_metadata={
                    "transforms": transforms,
                    "policies_invoked": invoked,
                    "policies_triggered": triggered,
                    "policies_skipped": skipped,
                },
            )
        return PolicyDecision(
            action=PolicyAction.ALLOW,
            reason_code="policy.allowed",
            reason="Allowed.",
            audit_metadata={
                "policies_invoked": invoked,
                "policies_triggered": triggered,
                "policies_skipped": skipped,
            },
        )

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from enum import Enum
from typing import Any, Protocol

import structlog
from opentelemetry import metrics, trace
from pydantic import BaseModel

from algen_agent_runtime.events.contracts import AuditEvent
from algen_agent_runtime.exceptions.errors import PolicyDeniedError
from algen_agent_runtime.policies.contracts import (
    POLICY_ERROR_REASON_CODE,
    PolicyAction,
    PolicyDecision,
    PolicyErrorHandling,
    PolicyFailMode,
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
    # ``Authorization: Bearer <token>``: a token has a digit or a separator, so prose such as
    # "bearer instruments" is left alone.
    _bearer = re.compile(
        r"(?i)\bbearer\s+((?=[A-Za-z0-9._~+/-]*[0-9._~+/-])[A-Za-z0-9._~+/-]{8,}=*)"
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
        if isinstance(value, Enum):
            # StrEnum members are str instances; rewriting them would drop the enum type.
            return value, False
        if isinstance(value, str):
            transformed = value
            count = 0
            for pattern in (self._pattern, self._bearer):
                transformed, found = pattern.subn(
                    lambda match: match.group(0).replace(match.group(1), "[REDACTED]"),
                    transformed,
                )
                count += found
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


_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
# 13 to 19 digits, optionally grouped by spaces or hyphens; redacted only when the Luhn check
# passes, so order numbers and timestamps are left alone.
_CARD = re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])")


# Issuer prefixes and lengths of the major card networks (Visa, Mastercard, American Express,
# Discover, JCB, Diners Club, UnionPay). Without them, one in ten 13-19 digit identifiers would
# pass the Luhn check: millisecond timestamps, order and tracking numbers.
_CARD_BRANDS = (
    (re.compile(r"4"), frozenset({13, 16, 19})),
    (re.compile(r"5[1-5]|2(?:2[2-9]|[3-6]\d|7[01]|720)"), frozenset({16})),
    (re.compile(r"3[47]"), frozenset({15})),
    (re.compile(r"6(?:011|5|4[4-9])"), frozenset({16, 19})),
    (re.compile(r"35(?:2[89]|[3-8]\d)"), frozenset({16, 19})),
    (re.compile(r"3(?:0[0-5]|[689])"), frozenset({14, 16, 19})),
    (re.compile(r"62"), frozenset({16, 17, 18, 19})),
)


def _is_card(number: str) -> bool:
    """A grouped or plain digit run that has a card network's prefix and length and passes
    the Luhn check."""
    digits = "".join(character for character in number if character.isdigit())
    return _luhn(digits) and any(
        prefix.match(digits) and len(digits) in lengths for prefix, lengths in _CARD_BRANDS
    )


def _luhn(number: str) -> bool:
    digits = [int(character) for character in number if character.isdigit()]
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


_TOOL_ARGUMENT_POINTS = frozenset({PolicyPoint.PLAN_TOOL, PolicyPoint.BEFORE_TOOL})


class _SchemaNode:
    """A position in a tool's JSON Schema, following ``properties``, ``items``, local
    ``$ref`` and ``anyOf``/``oneOf``/``allOf`` alternatives."""

    def __init__(self, nodes: tuple[Mapping[str, Any], ...], root: Mapping[str, Any]) -> None:
        self._nodes = nodes
        self._root = root

    @classmethod
    def root(cls, schema: Any) -> _SchemaNode | None:
        if not isinstance(schema, Mapping):
            return None
        return cls((schema,), schema)._expand()

    def _resolve(self, node: Mapping[str, Any], depth: int = 0) -> list[Mapping[str, Any]]:
        if depth > 16:
            return []
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/"):
            target: Any = self._root
            for part in reference[2:].split("/"):
                target = target.get(part) if isinstance(target, Mapping) else None
            if isinstance(target, Mapping):
                return self._resolve(target, depth + 1)
            return []
        resolved = [node]
        for keyword in ("anyOf", "oneOf", "allOf"):
            for branch in node.get(keyword) or ():
                if isinstance(branch, Mapping):
                    resolved.extend(self._resolve(branch, depth + 1))
        return resolved

    def _expand(self) -> _SchemaNode | None:
        nodes = tuple(item for node in self._nodes for item in self._resolve(node))
        return _SchemaNode(nodes, self._root) if nodes else None

    def _children(self, keyword: str, name: str | None = None) -> _SchemaNode | None:
        found: list[Mapping[str, Any]] = []
        for node in self._nodes:
            child = node.get(keyword)
            if name is not None:
                child = child.get(name) if isinstance(child, Mapping) else None
            if isinstance(child, Mapping):
                found.append(child)
        return _SchemaNode(tuple(found), self._root)._expand() if found else None

    def property(self, name: Any) -> _SchemaNode | None:
        return self._children("properties", str(name))

    def items(self) -> _SchemaNode | None:
        return self._children("items")

    def is_email(self) -> bool:
        return any(node.get("format") == "email" for node in self._nodes)


class PIIRedactionPolicy:
    """Redact common direct identifiers before they cross an agent boundary.

    The patterns are deliberately conservative. Deployments can register a specialised
    DLP policy for jurisdiction- or institution-specific identifiers.

    A tool argument whose input schema declares ``"format": "email"`` and whose value is
    exactly one email address is the tool's declared purpose (a recipient, say), so it is
    left as is; every other argument, such as a subject or body, is still redacted.
    """

    name = "pii"
    category = "pii"
    enforcement_mode = "warn"
    applies_to = _CONTENT_BOUNDARIES
    # Identifiers must stand alone: a digit run inside a UUID, hash or other token (a letter,
    # digit, underscore or hyphen next to it) is not a phone or Aadhaar number.
    _patterns = (
        ("email", _EMAIL),
        (
            "phone",
            re.compile(r"(?<![\w+-])(?:\+?91[\s.-]?)?[6-9]\d{4}[\s.-]?\d{5}(?![\w-])"),
        ),
        (
            "aadhaar",
            re.compile(r"(?<![\w-])[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}(?![\w-])"),
        ),
    )

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        schema: Any = None
        if point in _TOOL_ARGUMENT_POINTS:
            schema = getattr(context.get("tool"), "input_schema", None)
        transformed, labels = self._redact(payload, _SchemaNode.root(schema))
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

    def redact_value(self, value: Any) -> Any:
        """``value`` with the identifiers this policy finds replaced, at any depth."""
        return self._redact(value)[0]

    def _redact(self, value: Any, schema: _SchemaNode | None = None) -> tuple[Any, set[str]]:
        if isinstance(value, Enum):
            return value, set()
        if isinstance(value, str):
            if schema is not None and schema.is_email() and _EMAIL.fullmatch(value):
                return value, set()
            labels: set[str] = set()
            transformed = _CARD.sub(
                lambda match: "[REDACTED_CARD]" if _is_card(match.group(0)) else match.group(0),
                value,
            )
            if transformed != value:
                labels.add("card")
            for label, pattern in self._patterns:
                transformed, count = pattern.subn(f"[REDACTED_{label.upper()}]", transformed)
                if count:
                    labels.add(label)
            return transformed, labels
        if isinstance(value, BaseModel):
            updates: dict[str, Any] = {}
            model_labels: set[str] = set()
            for name in type(value).model_fields:
                item, item_labels = self._redact(
                    getattr(value, name), schema.property(name) if schema else None
                )
                updates[name] = item
                model_labels.update(item_labels)
            return value.model_copy(update=updates) if model_labels else value, model_labels
        if isinstance(value, Mapping):
            transformed_mapping: dict[Any, Any] = {}
            mapping_labels: set[str] = set()
            for key, item in value.items():
                transformed_item, item_labels = self._redact(
                    item, schema.property(key) if schema else None
                )
                transformed_mapping[key] = transformed_item
                mapping_labels.update(item_labels)
            return transformed_mapping if mapping_labels else value, mapping_labels
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            transformed_items = []
            sequence_labels: set[str] = set()
            for item in value:
                transformed_item, item_labels = self._redact(
                    item, schema.items() if schema else None
                )
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
                definition.requires_approval
                or (
                    approval_policy.require_for_destructive
                    and side_effect == SideEffect.DESTRUCTIVE
                )
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


INSTRUCTION_OVERRIDE_PATTERNS = (
    re.compile(r"(?i)\bignore\s+(?:all\s+)?(?:previous|prior|system|developer)\s+instructions?\b"),
    re.compile(
        r"(?i)\b(?:reveal|print|show|repeat|expose)\b.{0,40}\b(?:system|developer)\s+(?:prompt|message|instructions?)\b"
    ),
    re.compile(
        r"(?i)\b(?:bypass|disable|override)\b.{0,40}\b(?:guardrails?|safety|policy|instructions?)\b"
    ),
    re.compile(r"(?i)\b(?:jailbreak|do anything now|DAN mode)\b"),
)


def looks_like_instruction_override(text: str) -> bool:
    """High-signal attempts to override or disclose privileged instructions."""
    return any(pattern.search(text) for pattern in INSTRUCTION_OVERRIDE_PATTERNS)


class PromptInjectionPolicy:
    """Block clear attempts to override or disclose privileged instructions.

    This intentionally targets high-signal imperative attacks. Applications can
    replace it with a specialised classifier without changing orchestration.
    """

    name = "prompt_injection"
    category = "prompt_injection"
    enforcement_mode = "block"
    applies_to = frozenset({PolicyPoint.INPUT})
    _patterns = INSTRUCTION_OVERRIDE_PATTERNS

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


_TOOL_POINTS = frozenset({PolicyPoint.BEFORE_TOOL, PolicyPoint.AFTER_TOOL})
POLICY_ERROR_AUDIT_ACTION = "policy.error"
POLICY_ERROR_METRIC_NAME = "algen_agent_runtime.policy.errors"
_logger = structlog.get_logger("algen_agent_runtime.policies")


def _error_counter() -> Any:
    try:
        return metrics.get_meter("algen_agent_runtime.policies").create_counter(
            POLICY_ERROR_METRIC_NAME,
            unit="1",
            description="Policy evaluations that raised or timed out, by boundary and fail mode",
        )
    except Exception:
        return None


class _AuditLog(Protocol):
    async def append(self, event: AuditEvent) -> None: ...


class CompositePolicyEngine:
    def __init__(
        self,
        policies: Sequence[Policy] | None = None,
        guardrail_scope: Callable[..., AbstractContextManager[Any]] | None = None,
        *,
        error_handling: PolicyErrorHandling | None = None,
        audits: _AuditLog | None = None,
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
        # Authorization and side-effect approval are runtime security invariants; policies from
        # a policy bundle join them, so every agent runs them whatever its guardrail selection.
        self._mandatory: set[str] = {"authorization", "side_effects"}
        self.error_handling = error_handling or PolicyErrorHandling()
        self._audits = audits
        self._error_counter = _error_counter()
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

    def register(self, policy: Policy, *, replace: bool = False, mandatory: bool = False) -> None:
        """Add a policy. A ``mandatory`` one runs for every agent, whatever its selection."""
        if policy.name in self._policies and not replace:
            raise ValueError(f"policy {policy.name!r} is already registered")
        self._policies[policy.name] = policy
        if mandatory:
            self._mandatory.add(policy.name)

    def mandatory(self) -> tuple[str, ...]:
        return tuple(sorted(self._mandatory))

    async def _record_error(self, failure: dict[str, Any], context: Mapping[str, Any]) -> None:
        """Count, log and audit a policy that raised or timed out. Never the error's text: it
        may quote the payload."""
        attributes = {
            "boundary": failure["boundary"],
            "policy": failure["policy"],
            "fail_mode": failure["fail_mode"],
            "error_type": failure["error_type"],
        }
        if self._error_counter is not None:
            try:
                self._error_counter.add(1, attributes)
            except Exception:
                pass
        _logger.warning("policy_error", **attributes, timed_out=failure["timed_out"])
        tool_context = context.get("context")
        tenant_id = context.get("tenant_id") or getattr(tool_context, "tenant_id", None)
        if self._audits is None or not tenant_id:
            return
        user_id = context.get("user_id") or getattr(tool_context, "user_id", None)
        run_id = context.get("run_id") or getattr(tool_context, "run_id", None)
        tool = context.get("tool")
        metadata = {**failure}
        if tool is not None and getattr(tool, "name", None):
            metadata["tool"] = tool.name
        try:
            await self._audits.append(
                AuditEvent(
                    action=POLICY_ERROR_AUDIT_ACTION,
                    outcome=f"fail_{failure['fail_mode']}",
                    tenant_id=str(tenant_id),
                    actor_id=str(user_id or "system"),
                    resource_id=str(run_id or failure["boundary"]),
                    metadata=metadata,
                )
            )
        except Exception:
            _logger.exception("policy_error_audit_failed", **attributes)

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
        guardrails = getattr(agent, "guardrail_policy", None)
        selected = set(getattr(guardrails, "policies", ()))
        tool_policies = getattr(guardrails, "tool_policies", None)
        if tool_policies is not None and policy_point in _TOOL_POINTS:
            selected = set(tool_policies)
        selected.update(self._mandatory)
        invoked: list[str] = []
        triggered: list[str] = []
        skipped: list[str] = []
        errors: list[dict[str, Any]] = []
        pending: PolicyDecision | None = None
        timeout = self.error_handling.timeout_seconds
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
                try:
                    async with asyncio.timeout(timeout):
                        decision = await policy.evaluate(point, transformed, context)
                    if not isinstance(decision, PolicyDecision):
                        raise TypeError("policy returned something other than a PolicyDecision")
                except PolicyDeniedError:
                    # A policy that raises a denial has decided; it is not a policy error.
                    raise
                except Exception as exc:
                    mode = self.error_handling.mode(policy_point)
                    failure = {
                        "boundary": policy_point.value,
                        "policy": policy.name,
                        "error_type": type(exc).__name__,
                        "fail_mode": mode.value,
                        "timed_out": isinstance(exc, TimeoutError),
                    }
                    guardrail_span.set_attribute("guardrail.error", failure["error_type"])
                    guardrail_span.set_attribute("guardrail.fail_mode", mode.value)
                    await self._record_error(failure, context)
                    errors.append(failure)
                    if mode == PolicyFailMode.OPEN:
                        continue
                    return PolicyDecision(
                        action=PolicyAction.DENY,
                        reason_code=POLICY_ERROR_REASON_CODE,
                        reason=(
                            f"Policy {policy.name} could not decide at {policy_point.value}; "
                            "this boundary fails closed."
                        ),
                        audit_metadata={
                            "fail_mode": mode.value,
                            "policy_errors": errors,
                            "policies_invoked": invoked,
                            "policies_triggered": [*triggered, policy.name],
                            "policies_skipped": skipped,
                        },
                    )
                triggered_now = decision.action != PolicyAction.ALLOW
                guardrail_span.set_attribute("guardrail.triggered", triggered_now)
                guardrail_span.set_attribute("guardrail.reason_code", decision.reason_code)
            if decision.action == PolicyAction.DENY:
                return decision.model_copy(
                    update={
                        "audit_metadata": {
                            **decision.audit_metadata,
                            "policies_invoked": invoked,
                            "policies_triggered": [*triggered, policy.name],
                            "policies_skipped": skipped,
                            **self._errors_metadata(errors),
                        }
                    }
                )
            if decision.action in {
                PolicyAction.REQUIRE_CLARIFICATION,
                PolicyAction.REQUIRE_APPROVAL,
            }:
                # The first such answer stands, but the remaining policies still run: a later
                # denial must refuse the call rather than put it to a person first.
                triggered.append(policy.name)
                if pending is None:
                    pending = decision
                continue
            if decision.action in {PolicyAction.REDACT, PolicyAction.TRANSFORM}:
                transformed = decision.value
                transforms.append(decision.reason_code)
                triggered.append(policy.name)
        if pending is not None:
            return pending.model_copy(
                update={
                    "audit_metadata": {
                        **pending.audit_metadata,
                        "policies_invoked": invoked,
                        "policies_triggered": triggered,
                        "policies_skipped": skipped,
                        **self._errors_metadata(errors),
                    }
                }
            )
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
                    **self._errors_metadata(errors),
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
                **self._errors_metadata(errors),
            },
        )

    @staticmethod
    def _errors_metadata(errors: Sequence[dict[str, Any]]) -> dict[str, Any]:
        # Only present when a policy failed open, so ordinary decisions read as they always did.
        if not errors:
            return {}
        return {"fail_mode": PolicyFailMode.OPEN.value, "policy_errors": list(errors)}

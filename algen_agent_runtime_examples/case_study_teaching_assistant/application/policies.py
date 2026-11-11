from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from algen_agent_runtime.policies.contracts import PolicyAction, PolicyDecision, PolicyPoint


class AcademicIntegrityPolicy:
    """Keep assessment help in tutor mode without abandoning the learner."""

    name = "academic_integrity"
    category = "input_validation"
    enforcement_mode = "warn"
    applies_to = frozenset({PolicyPoint.INPUT})
    _assessment = re.compile(
        r"(?i)\b(?:graded|exam|quiz|take[- ]home|assignment|homework|submit|answer key)\b"
    )
    _answer_request = re.compile(
        r"(?i)\b(?:give|write|solve|complete|do|provide|tell)\b.{0,50}"
        r"\b(?:answer|solution|submission|essay|code|homework|assignment)\b"
    )

    def triggered(self, value: str) -> bool:
        return bool(self._assessment.search(value) and self._answer_request.search(value))

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        if point != "input" or not isinstance(payload, str) or not self.triggered(payload):
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                reason_code="academic_integrity.clear",
                reason="No direct assessed-work request detected.",
            )
        return PolicyDecision(
            action=PolicyAction.TRANSFORM,
            reason_code="academic_integrity.tutor_mode",
            reason="Direct assessed-work completion was converted to guided tutoring.",
            value=(
                "[ACADEMIC INTEGRITY: TUTOR MODE] The learner appears to be asking for a direct "
                "answer to assessed work. Do not supply a submission-ready final answer. Explain the "
                "relevant concept, ask one diagnostic question, and provide a hint or analogous example. "
                f"Learner request: {payload}"
            ),
            audit_metadata={"risk": "assessed_work", "intervention": "tutor_mode"},
        )


class EducationalEquityPolicy:
    """Prevent unsupported ability claims about protected or disadvantaged groups."""

    name = "educational_equity"
    category = "moderation"
    enforcement_mode = "warn"
    applies_to = frozenset({PolicyPoint.INPUT})
    _group = re.compile(
        r"(?i)\b(?:women|men|girls|boys|caste|religion|religious|rural|urban|disabled|"
        r"disability|tribal|ethnic|low[- ]income|first[- ]generation)\b"
    )
    _ability = re.compile(
        r"(?i)\b(?:less|more|better|worse|weak|strong|capable|intelligent|smart|perform)\w*\b"
    )

    def triggered(self, value: str) -> bool:
        return bool(self._group.search(value) and self._ability.search(value))

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        if point != "input" or not isinstance(payload, str) or not self.triggered(payload):
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                reason_code="educational_equity.clear",
                reason="No unsupported group-level ability inference detected.",
            )
        return PolicyDecision(
            action=PolicyAction.TRANSFORM,
            reason_code="educational_equity.reframed",
            reason="A group-level ability claim was reframed for equitable analysis.",
            value=(
                "[EDUCATIONAL EQUITY] Do not endorse inherent ability claims about demographic groups. "
                "Reframe the discussion around evidence quality, structural conditions, access, measurement "
                "bias, within-group variation, and fair evaluation. "
                f"Learner request: {payload}"
            ),
            audit_metadata={"risk": "group_generalization", "intervention": "equity_reframe"},
        )

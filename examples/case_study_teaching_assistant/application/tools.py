from __future__ import annotations

from typing import Any

from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)
from algen_agent_runtime.tools.registry import ToolRegistry


def register_teaching_tools(registry: ToolRegistry) -> None:
    """Register deterministic demo tools through the normal Runtime tool SDK."""

    registry.register(
        Tool(
            ToolDefinition(
                name="teaching.build_study_plan",
                version="1.0.0",
                description=(
                    "Build a practical revision timetable. Use this whenever a learner gives a "
                    "topic, number of days, and daily study time and asks for a study or revision plan."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string", "minLength": 1},
                        "days": {"type": "integer", "minimum": 3, "maximum": 90},
                        "minutes_per_day": {"type": "integer", "minimum": 15, "maximum": 480},
                    },
                    "required": ["topic", "days", "minutes_per_day"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "phases": {"type": "array", "items": {"type": "object"}},
                        "daily_minutes": {"type": "integer"},
                        "method": {"type": "string"},
                    },
                    "required": ["topic", "phases", "daily_minutes", "method"],
                    "additionalProperties": False,
                },
                required_permissions=frozenset({"learning.plan.read"}),
                side_effect=SideEffect.READ,
                idempotency=Idempotency.IDEMPOTENT,
                timeout_seconds=2,
                max_concurrency=20,
                audit_metadata={"source": "demo-course-planner", "mode": "deterministic"},
            ),
            _build_study_plan,
        )
    )
    registry.register(
        Tool(
            ToolDefinition(
                name="teaching.get_case_evidence",
                version="1.0.0",
                description=(
                    "Retrieve a compact evidence card for a real-world AI safety or society case. "
                    "Use for facial-recognition attendance, automated proctoring, or claims about "
                    "an AI tutor's learning impact before analysing the case."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "case_id": {
                            "type": "string",
                            "enum": [
                                "facial_recognition_attendance",
                                "automated_proctoring",
                                "ai_tutor_learning_claim",
                            ],
                        }
                    },
                    "required": ["case_id"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "case_id": {"type": "string"},
                        "observations": {"type": "array", "items": {"type": "string"}},
                        "missing_evidence": {"type": "array", "items": {"type": "string"}},
                        "decision_questions": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "case_id",
                        "observations",
                        "missing_evidence",
                        "decision_questions",
                    ],
                    "additionalProperties": False,
                },
                required_permissions=frozenset({"learning.case.read"}),
                side_effect=SideEffect.READ,
                idempotency=Idempotency.IDEMPOTENT,
                timeout_seconds=2,
                max_concurrency=20,
                audit_metadata={"source": "demo-case-library", "mode": "deterministic"},
            ),
            _get_case_evidence,
        )
    )
    registry.register(
        Tool(
            ToolDefinition(
                name="teaching.lookup_learning_policy",
                version="1.0.0",
                description=(
                    "Look up the teaching assistant's learning policy. Use when a learner asks for "
                    "help with graded work, data sharing, consent, or acceptable AI assistance."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "enum": ["academic_integrity", "privacy_and_consent", "ai_assistance"],
                        }
                    },
                    "required": ["topic"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "allowed": {"type": "array", "items": {"type": "string"}},
                        "not_allowed": {"type": "array", "items": {"type": "string"}},
                        "student_action": {"type": "string"},
                    },
                    "required": ["topic", "allowed", "not_allowed", "student_action"],
                    "additionalProperties": False,
                },
                required_permissions=frozenset({"learning.policy.read"}),
                side_effect=SideEffect.READ,
                idempotency=Idempotency.IDEMPOTENT,
                timeout_seconds=2,
                max_concurrency=20,
                audit_metadata={"source": "demo-learning-policy", "mode": "deterministic"},
            ),
            _lookup_learning_policy,
        )
    )


def _build_study_plan(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    del context
    days = int(arguments["days"])
    review_days = max(1, round(days * 0.2))
    practice_days = max(1, round(days * 0.3))
    learn_days = max(1, days - review_days - practice_days)
    return {
        "topic": arguments["topic"],
        "daily_minutes": arguments["minutes_per_day"],
        "method": "retrieval practice with spaced review",
        "phases": [
            {"days": f"1-{learn_days}", "focus": "concept map, worked examples, short recall"},
            {
                "days": f"{learn_days + 1}-{learn_days + practice_days}",
                "focus": "mixed practice, error log, targeted revision",
            },
            {
                "days": f"{learn_days + practice_days + 1}-{days}",
                "focus": "timed retrieval, weak areas, final self-check",
            },
        ],
    }


_CASES: dict[str, dict[str, list[str]]] = {
    "facial_recognition_attendance": {
        "observations": [
            "Reported overall accuracy is 96%.",
            "Error rates are higher for darker-skinned students.",
            "Attendance can affect access, discipline, and academic records.",
        ],
        "missing_evidence": [
            "False-match and false-non-match rates by relevant groups.",
            "Performance under classroom lighting and camera conditions.",
            "Consent, appeal, human review, retention, and non-biometric alternatives.",
        ],
        "decision_questions": [
            "Who bears the cost of an error?",
            "Can a student opt out without disadvantage?",
            "Is a less intrusive method adequate?",
        ],
    },
    "automated_proctoring": {
        "observations": [
            "The proposed system records webcam, microphone, and browser activity during assessments.",
            "Automated flags may affect grades or misconduct investigations if treated as conclusions.",
        ],
        "missing_evidence": [
            "False-positive rates across disability, skin tone, device, bandwidth, and home conditions.",
            "Human review, appeal, retention, access, deletion, and less intrusive alternatives.",
        ],
        "decision_questions": [
            "Is continuous surveillance necessary and proportionate to the assessment risk?",
            "Can students challenge a flag before any adverse decision?",
        ],
    },
    "ai_tutor_learning_claim": {
        "observations": [
            "An article claims a 40% improvement in marks.",
            "A percentage alone does not identify baseline, sample, duration, or comparison group.",
        ],
        "missing_evidence": [
            "Study design, sample size, attrition, outcome definition, and uncertainty.",
            "Independent replication and conflicts of interest.",
        ],
        "decision_questions": [
            "Was the comparison randomised or otherwise controlled?",
            "Is the measured outcome educationally meaningful?",
        ],
    },
}


def _get_case_evidence(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    del context
    case_id = str(arguments["case_id"])
    return {"case_id": case_id, **_CASES[case_id]}


_POLICIES: dict[str, dict[str, Any]] = {
    "academic_integrity": {
        "allowed": ["concept explanations", "hints", "feedback on learner-authored work"],
        "not_allowed": ["submission-ready answers for active graded assessments"],
        "student_action": "Share your attempt or ask for a hint on the step where you are stuck.",
    },
    "privacy_and_consent": {
        "allowed": ["necessary data with clear purpose and informed consent"],
        "not_allowed": ["coerced consent", "unnecessary collection", "hidden secondary use"],
        "student_action": "Deny optional permissions until purpose, retention, and deletion are clear.",
    },
    "ai_assistance": {
        "allowed": ["brainstorming", "practice", "explanation", "editing with disclosure"],
        "not_allowed": ["misrepresenting generated work as solely your own"],
        "student_action": "Follow the course disclosure rule and retain evidence of your own work.",
    },
}


def _lookup_learning_policy(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    del context
    topic = str(arguments["topic"])
    return {"topic": topic, **_POLICIES[topic]}

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from algen_agent_runtime.conversations import Conversation, ConversationMessage
from algen_agent_runtime.security.documents import DocumentSecurityFinding, FindingSeverity
from algen_agent_runtime.types.contracts import Role
from examples.case_study_responsible_hiring.application.contracts import HiringApplication
from examples.case_study_responsible_hiring.application.repositories import InMemoryHiringRepository
from examples.case_study_responsible_hiring.application.workflow import (
    HiringConversationHandler,
    HiringWorkflow,
)


class StructuredClient:
    async def run(self, request: Any) -> Any:
        outputs = {
            "hiring-gatekeeper": {
                "safe_to_continue": True,
                "injection_detected": False,
                "risk_level": "low",
                "findings": [],
                "redaction_categories": ["name", "email"],
                "rationale": "No manipulation found.",
            },
            "hiring-fraud-reviewer": {
                "risk_level": "low",
                "signals": [],
                "requires_human_review": False,
                "summary": "No material integrity signals.",
            },
            "hiring-screening": {
                "relevance_score": 88,
                "supported_skills": ["Python", "PostgreSQL"],
                "missing_skills": ["Kubernetes"],
                "evidence": [
                    {"claim": "Python", "evidence": "Built Python APIs", "confidence": 0.9}
                ],
                "summary": "Strong evidence for critical requirements.",
                "recommendation": "advance",
                "confidence": 0.86,
            },
            "hiring-bias-observer": {
                "approved": True,
                "prohibited_factors_found": [],
                "unsupported_reasons": [],
                "corrected_summary": None,
                "rationale": "Reasons are job-related.",
            },
            "hiring-communications": {
                "kind": "interview_invitation",
                "subject": "Interview invitation",
                "body": "We would like to discuss your relevant experience.",
                "disclosures": ["Human approval required"],
            },
            "hiring-review-assistant": {
                "answer": "The document was quarantined because deterministic security checks found an instruction-override pattern.",
                "evidence_basis": ["prompt_injection.instruction_override"],
                "limitations": ["The suspicious payload is intentionally not repeated."],
            },
        }
        return SimpleNamespace(
            error=None, output=json.dumps(outputs[request.agent]), run_id=request.agent
        )


async def test_hiring_workflow_fans_out_and_requires_human_approval() -> None:
    repository = InMemoryHiringRepository()
    application = HiringApplication(
        tenant_id="tenant-a",
        user_id="recruiter-a",
        candidate_name="Restricted Name",
        job_title="AI Engineer",
        job_description="Python PostgreSQL security and testing",
        sanitized_resume="Built Python APIs backed by PostgreSQL with pytest.",
        document_sha256="0" * 64,
    )
    await repository.save_application(application)
    events: list[str] = []

    async def emit(kind: str, data: dict[str, Any] | None = None) -> None:
        del data
        events.append(kind)

    assessment = await HiringWorkflow(StructuredClient(), repository).evaluate(
        application, conversation_id="conversation-a", turn_id="turn-a", emit=emit
    )

    assert assessment.screening is not None
    assert assessment.screening.relevance_score == 88
    assert assessment.final_recommendation.value == "advance"
    assert assessment.human_approval_required is True
    assert len(assessment.run_ids) == 5
    assert events[-1] == "workflow.progress"


async def test_hiring_handler_answers_followup_without_rerunning_screening() -> None:
    repository = InMemoryHiringRepository()
    application = HiringApplication(
        tenant_id="tenant-a",
        user_id="recruiter-a",
        candidate_name="Restricted Name",
        job_title="AI Engineer",
        job_description="Python PostgreSQL security and testing",
        sanitized_resume="Built Python APIs backed by PostgreSQL with pytest.",
        document_sha256="0" * 64,
    )
    await repository.save_application(application)

    async def emit(kind: str, data: dict[str, Any] | None = None) -> None:
        del kind, data

    workflow = HiringWorkflow(StructuredClient(), repository)
    assessment = await workflow.evaluate(
        application, conversation_id="conversation-a", turn_id="turn-a", emit=emit
    )
    conversation = Conversation(
        id="conversation-a", tenant_id="tenant-a", user_id="recruiter-a", agent="hiring"
    )
    previous = ConversationMessage.text(
        conversation_id=conversation.id,
        tenant_id=conversation.tenant_id,
        role=Role.ASSISTANT,
        text="Assessment complete",
        metadata={"assessment": assessment.model_dump(mode="json")},
    )
    question = ConversationMessage.text(
        conversation_id=conversation.id,
        tenant_id=conversation.tenant_id,
        role=Role.USER,
        text="Why was this quarantined?",
    )

    result = await HiringConversationHandler(workflow, repository).handle(
        conversation, question, (previous, question), emit
    )

    assert "deterministic security checks" in result.content[0].text
    assert result.metadata["kind"] == "review_followup"
    assert result.run_ids == ("hiring-review-assistant",)


async def test_quarantine_assessment_exposes_safe_finding_summary_without_payload() -> None:
    class QuarantineClient:
        async def run(self, request: Any) -> Any:
            del request
            return SimpleNamespace(
                error=None,
                run_id="gatekeeper-run",
                output=json.dumps(
                    {
                        "safe_to_continue": False,
                        "injection_detected": True,
                        "risk_level": "high",
                        "findings": ["Instruction override attempt"],
                        "redaction_categories": ["name"],
                        "rationale": "The document attempted to alter screening instructions.",
                    }
                ),
            )

    repository = InMemoryHiringRepository()
    application = HiringApplication(
        tenant_id="tenant-a",
        user_id="recruiter-a",
        candidate_name="Restricted Name",
        job_title="AI Engineer",
        job_description="Python and security",
        sanitized_resume="Python engineer",
        document_sha256="0" * 64,
        document_findings=(
            DocumentSecurityFinding(
                kind="prompt_injection.instruction_override",
                severity=FindingSeverity.CRITICAL,
                message="The document contains text resembling instructions to an AI system.",
                page=1,
                evidence="do not display this payload",
            ),
        ),
        quarantined=True,
    )

    async def emit(kind: str, data: dict[str, Any] | None = None) -> None:
        del kind, data

    assessment = await HiringWorkflow(QuarantineClient(), repository).evaluate(
        application, conversation_id="conversation-a", turn_id="turn-a", emit=emit
    )

    assert assessment.screening is None
    assert assessment.security_findings[0].kind == "prompt_injection.instruction_override"
    assert "payload" not in assessment.model_dump_json()

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.security.documents import DocumentSecurityFinding
from algen_agent_runtime.types.contracts import utc_now


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class HiringRecommendation(StrEnum):
    ADVANCE = "advance"
    REVIEW = "review"
    DECLINE = "decline"


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    claim: str
    evidence: str
    confidence: float = Field(ge=0, le=1)


class GatekeeperReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    safe_to_continue: bool
    injection_detected: bool
    risk_level: RiskLevel
    findings: tuple[str, ...]
    redaction_categories: tuple[str, ...]
    rationale: str


class FraudSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["timeline", "template_similarity", "claim_depth", "other"]
    severity: RiskLevel
    evidence: str
    alternative_explanation: str
    confidence: float = Field(ge=0, le=1)


class FraudReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    risk_level: RiskLevel
    signals: tuple[FraudSignal, ...]
    requires_human_review: bool
    summary: str


class ScreeningReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    relevance_score: int = Field(ge=0, le=100)
    supported_skills: tuple[str, ...]
    missing_skills: tuple[str, ...]
    evidence: tuple[EvidenceItem, ...]
    summary: str
    recommendation: HiringRecommendation
    confidence: float = Field(ge=0, le=1)


class BiasReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    approved: bool
    prohibited_factors_found: tuple[str, ...]
    unsupported_reasons: tuple[str, ...]
    corrected_summary: str | None
    rationale: str


class CommunicationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["interview_invitation", "constructive_feedback", "manual_review"]
    subject: str
    body: str
    disclosures: tuple[str, ...]


class ReviewAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    answer: str
    evidence_basis: tuple[str, ...]
    limitations: tuple[str, ...]


class SecurityFindingSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: str
    severity: str
    message: str
    page: int | None = None


class HiringApplication(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str
    user_id: str
    candidate_name: str
    job_title: str
    job_description: str
    sanitized_resume: str
    document_sha256: str
    document_findings: tuple[DocumentSecurityFinding, ...] = ()
    quarantined: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class HiringAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: str(uuid4()))
    application_id: str
    tenant_id: str
    gatekeeper: GatekeeperReport
    security_findings: tuple[SecurityFindingSummary, ...] = ()
    fraud: FraudReport | None
    screening: ScreeningReport | None
    bias: BiasReport | None
    communication: CommunicationDraft | None
    final_recommendation: HiringRecommendation
    human_approval_required: bool = True
    run_ids: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GovernanceDecisionType(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class ExportAction(StrEnum):
    VIEW_SQL = "view_sql"
    DOWNLOAD = "download"
    EXPORT_ARTIFACT = "export_artifact"


class DataTrustRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: str
    observed_at: datetime | None = None
    maximum_age_seconds: int | None = Field(default=None, gt=0)
    completeness: float | None = Field(default=None, ge=0, le=1)
    minimum_completeness: float | None = Field(default=None, ge=0, le=1)
    certified: bool = False
    lineage: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class QueryEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    estimated_rows: int = Field(default=0, ge=0)
    estimated_bytes_scanned: int = Field(default=0, ge=0)
    estimated_compute_seconds: float = Field(default=0, ge=0)


class QueryGovernancePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    allowed_purposes: tuple[str, ...] = ()
    required_authorization_tags: tuple[str, ...] = ()
    denied_columns: tuple[str, ...] = ()
    allowed_metrics: tuple[str, ...] = ()
    require_certified_sources: bool = True
    maximum_rows: int = Field(default=10_000, ge=1)
    maximum_bytes_scanned: int = Field(default=1_000_000_000, ge=1)
    maximum_compute_seconds: float = Field(default=30, gt=0)
    maximum_concurrency: int = Field(default=4, ge=1, le=128)
    allow_sql_visibility: bool = False
    allow_download: bool = False
    allow_artifact_export: bool = False
    audit_retention_days: int = Field(default=365, ge=1)


class GovernedQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: str
    user_id: str
    purpose: str
    source_ids: tuple[str, ...] = Field(min_length=1)
    columns: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()
    authorization_tags: tuple[str, ...] = ()
    trust: tuple[DataTrustRequirement, ...] = ()
    estimate: QueryEstimate = Field(default_factory=QueryEstimate)
    requested_actions: tuple[ExportAction, ...] = ()
    query_fingerprint: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GovernanceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    decision: GovernanceDecisionType
    reason_code: str
    reasons: tuple[str, ...] = ()
    audit_metadata: dict[str, Any] = Field(default_factory=dict)

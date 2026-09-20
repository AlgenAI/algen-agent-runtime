from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NumericExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    expected: float
    absolute_tolerance: float = Field(default=0, ge=0)
    relative_tolerance: float = Field(default=0, ge=0)


class GoldenQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    question: str
    expected_node_kinds: tuple[str, ...] = ()
    required_sql_patterns: tuple[str, ...] = ()
    forbidden_sql_patterns: tuple[str, ...] = ()
    numeric_expectations: tuple[NumericExpectation, ...] = ()
    allowed_claims: tuple[str, ...] = ()
    forbidden_claims: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_kinds: tuple[str, ...]
    sql: tuple[str, ...]
    values: dict[str, Any]
    claims: tuple[str, ...]


class CaseEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: str
    score: float = Field(ge=0, le=1)
    passed: bool
    checks: dict[str, bool]
    failures: tuple[str, ...] = ()


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cases: tuple[CaseEvaluation, ...]
    score: float = Field(ge=0, le=1)
    promoted: bool
    promotion_threshold: float = Field(ge=0, le=1)

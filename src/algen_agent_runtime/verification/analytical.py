from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.analytics import AnalyticalResult


class CellReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    result_node_id: str
    row: int = Field(ge=0)
    column: str


class AnalyticalClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str
    value: float | str | bool | None = None
    evidence: tuple[CellReference, ...] = Field(min_length=1)
    metric: str | None = None
    metric_certification: str | None = None
    model_version: str | None = None


class AnalyticalVerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    valid: bool
    errors: tuple[str, ...] = ()


class AnalyticalVerifier:
    """Deterministic claim-to-cell, reconciliation, sample and model-version checks."""

    def verify_claims(
        self,
        claims: tuple[AnalyticalClaim, ...],
        results: dict[str, AnalyticalResult],
        *,
        minimum_metric_certification: str = "verified",
        required_model_version: str | None = None,
    ) -> AnalyticalVerificationResult:
        errors: list[str] = []
        rank = {"draft": 0, "verified": 1, "certified": 2}
        for index, claim in enumerate(claims):
            cells: list[Any] = []
            for evidence in claim.evidence:
                result = results.get(evidence.result_node_id)
                if (
                    result is None
                    or not isinstance(result.value, list)
                    or evidence.row >= len(result.value)
                ):
                    errors.append(f"claim[{index}] references a missing result row")
                    continue
                row = result.value[evidence.row]
                if not isinstance(row, dict) or evidence.column not in row:
                    errors.append(f"claim[{index}] references a missing result column")
                    continue
                cells.append(row[evidence.column])
            if claim.value is not None and len(cells) == 1:
                if isinstance(claim.value, float) and isinstance(cells[0], (int, float)):
                    if not math.isclose(claim.value, float(cells[0]), rel_tol=1e-9):
                        errors.append(f"claim[{index}] does not reconcile to its evidence cell")
                elif claim.value != cells[0]:
                    errors.append(f"claim[{index}] does not equal its evidence cell")
            if (
                claim.metric
                and rank.get(claim.metric_certification or "draft", -1)
                < rank[minimum_metric_certification]
            ):
                errors.append(f"claim[{index}] uses an insufficiently certified metric")
            if required_model_version and claim.model_version != required_model_version:
                errors.append(f"claim[{index}] uses the wrong model version")
        return AnalyticalVerificationResult(valid=not errors, errors=tuple(errors))

    @staticmethod
    def validate_sample(result: AnalyticalResult, minimum_sample_size: int) -> None:
        if (result.row_count or 0) < minimum_sample_size:
            raise ValueError(
                f"result {result.node_id!r} has fewer than {minimum_sample_size} observations"
            )

    @staticmethod
    def validate_comparison_windows(
        current_start: str, current_end: str, baseline_start: str, baseline_end: str
    ) -> None:
        from datetime import date

        current_days = (date.fromisoformat(current_end) - date.fromisoformat(current_start)).days
        baseline_days = (date.fromisoformat(baseline_end) - date.fromisoformat(baseline_start)).days
        if current_days != baseline_days:
            raise ValueError("comparison windows must have equal duration")

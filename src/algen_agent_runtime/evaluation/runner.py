from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from opentelemetry import metrics, trace

from algen_agent_runtime.evaluation.contracts import (
    CaseEvaluation,
    EvaluationArtifact,
    EvaluationReport,
    GoldenQuestion,
)

CaseExecutor = Callable[[GoldenQuestion], Awaitable[EvaluationArtifact]]


class EvaluationRunner:
    def __init__(self) -> None:
        self._tracer = trace.get_tracer("algen_agent_runtime.evaluation")
        self._counter = metrics.get_meter("algen_agent_runtime.evaluation").create_counter(
            "algen_agent_runtime.evaluation.cases"
        )

    async def run(
        self,
        cases: Sequence[GoldenQuestion],
        executor: CaseExecutor,
        *,
        promotion_threshold: float = 0.9,
    ) -> EvaluationReport:
        results: list[CaseEvaluation] = []
        for case in cases:
            with self._tracer.start_as_current_span(
                "evaluation.case", attributes={"evaluation.case_id": case.id}
            ):
                artifact = await executor(case)
                result = self.evaluate(case, artifact)
                results.append(result)
                self._counter.add(1, {"outcome": "passed" if result.passed else "failed"})
        score = sum(item.score for item in results) / len(results) if results else 0
        return EvaluationReport(
            cases=tuple(results),
            score=score,
            promoted=bool(results)
            and score >= promotion_threshold
            and all(item.passed for item in results),
            promotion_threshold=promotion_threshold,
        )

    @staticmethod
    def evaluate(case: GoldenQuestion, artifact: EvaluationArtifact) -> CaseEvaluation:
        checks: dict[str, bool] = {}
        checks["plan"] = all(item in artifact.node_kinds for item in case.expected_node_kinds)
        sql_text = "\n".join(artifact.sql)
        checks["sql_required"] = all(
            re.search(pattern, sql_text, re.IGNORECASE) is not None
            for pattern in case.required_sql_patterns
        )
        checks["sql_forbidden"] = all(
            re.search(pattern, sql_text, re.IGNORECASE) is None
            for pattern in case.forbidden_sql_patterns
        )
        numeric = True
        for expectation in case.numeric_expectations:
            try:
                actual = float(_path(artifact.values, expectation.path))
            except (KeyError, TypeError, ValueError):
                numeric = False
                break
            numeric = numeric and math.isclose(
                actual,
                expectation.expected,
                rel_tol=expectation.relative_tolerance,
                abs_tol=expectation.absolute_tolerance,
            )
        checks["numeric"] = numeric
        normalized_claims = " ".join(artifact.claims).lower()
        checks["allowed_claims"] = all(
            claim.lower() in normalized_claims for claim in case.allowed_claims
        )
        checks["forbidden_claims"] = all(
            claim.lower() not in normalized_claims for claim in case.forbidden_claims
        )
        score = sum(checks.values()) / len(checks)
        failures = tuple(name for name, passed in checks.items() if not passed)
        return CaseEvaluation(
            case_id=case.id,
            score=score,
            passed=not failures,
            checks=checks,
            failures=failures,
        )


def _path(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current

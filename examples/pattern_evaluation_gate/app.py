from __future__ import annotations

import asyncio
import json

from algen_agent_runtime.evaluation import EvaluationArtifact, EvaluationRunner, GoldenQuestion

CASES = (
    GoldenQuestion(
        id="aggregate",
        question="Count completed orders",
        expected_node_kinds=("query",),
        required_sql_patterns=(r"select", r"count"),
        forbidden_sql_patterns=(r"delete|update|insert",),
        allowed_claims=("read only",),
    ),
    GoldenQuestion(
        id="reject-write",
        question="Delete cancelled orders",
        expected_node_kinds=("refusal",),
        forbidden_sql_patterns=(r"delete",),
        allowed_claims=("refused",),
    ),
)


async def execute(case: GoldenQuestion) -> EvaluationArtifact:
    if case.id == "aggregate":
        return EvaluationArtifact(
            node_kinds=("query",),
            sql=("SELECT COUNT(*) FROM orders WHERE status = 'completed'",),
            values={},
            claims=("Read only query validated",),
        )
    return EvaluationArtifact(
        node_kinds=("refusal",), sql=(), values={}, claims=("Write request refused",)
    )


async def run_gate() -> int:
    report = await EvaluationRunner().run(CASES, execute, promotion_threshold=1.0)
    print(json.dumps(report.model_dump(mode="json"), indent=2))
    return 0 if report.promoted else 1


def main() -> None:
    raise SystemExit(asyncio.run(run_gate()))


if __name__ == "__main__":
    main()

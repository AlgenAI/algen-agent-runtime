from __future__ import annotations

from algen_agent_runtime.evaluation import (
    EvaluationArtifact,
    EvaluationRunner,
    GoldenQuestion,
    NumericExpectation,
)
from algen_agent_runtime.model_services import (
    DeterministicModelService,
    DriftState,
    ModelServiceKind,
    ModelServiceManifest,
    ModelServiceRegistry,
    ModelServiceRequest,
)


async def test_model_service_is_versioned_validated_and_batched() -> None:
    registry = ModelServiceRegistry()
    registry.register(
        DeterministicModelService(
            ModelServiceManifest(
                name="demand_forecast",
                version="2.1.0",
                kind=ModelServiceKind.FORECAST,
                description="test",
                input_schema={"type": "object", "required": ["value"]},
                output_schema={"type": "object", "required": ["prediction"]},
                drift_state=DriftState.HEALTHY,
            ),
            lambda row, parameters: {"prediction": row["value"] * parameters["factor"]},
        )
    )
    response = await registry.invoke(
        ModelServiceRequest(
            model="demand_forecast",
            version="2.1.0",
            inputs=({"value": 2}, {"value": 3}),
            parameters={"factor": 2},
        )
    )
    assert response.outputs == ({"prediction": 4}, {"prediction": 6})


async def test_golden_evaluation_applies_promotion_gate() -> None:
    case = GoldenQuestion(
        id="revenue-by-route",
        question="Revenue by route",
        expected_node_kinds=("semantic_query",),
        required_sql_patterns=(r"sum\(revenue\)",),
        forbidden_sql_patterns=(r"delete",),
        numeric_expectations=(NumericExpectation(path="total", expected=10),),
        allowed_claims=("10",),
        forbidden_claims=("caused",),
    )

    async def execute(_case):
        return EvaluationArtifact(
            node_kinds=("semantic_query",),
            sql=("SELECT SUM(revenue) FROM sales",),
            values={"total": 10},
            claims=("Revenue was 10",),
        )

    report = await EvaluationRunner().run((case,), execute, promotion_threshold=1)
    assert report.promoted
    assert report.score == 1

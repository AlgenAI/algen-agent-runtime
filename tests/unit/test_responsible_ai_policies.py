from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from algen_agent_runtime.policies.contracts import PolicyAction
from algen_agent_runtime.policies.engine import CompositePolicyEngine, PIIRedactionPolicy
from algen_agent_runtime.types.contracts import AgentDefinition
from examples.case_study_teaching_assistant.application.policies import (
    AcademicIntegrityPolicy,
    EducationalEquityPolicy,
)


def _agent() -> AgentDefinition:
    return AgentDefinition.model_validate(
        {
            "name": "policy-test-agent",
            "version": "1.0.0",
            "description": "test",
            "system_instructions": "test",
            "default_model": {"name": "test", "provider": "mock", "model": "mock"},
            "guardrail_policy": {
                "policies": ["pii", "academic_integrity"],
            },
            "budget": {"max_tokens": 10, "max_cost_usd": 1},
        }
    )


async def test_pii_policy_redacts_email_phone_and_aadhaar() -> None:
    decision = await PIIRedactionPolicy().evaluate(
        "input",
        "Contact learner@example.com, +91 98765 43210, Aadhaar 2345-6789-1234",
        {},
    )

    assert decision.action == PolicyAction.REDACT
    assert "learner@example.com" not in decision.value
    assert "98765" not in decision.value
    assert "2345-6789-1234" not in decision.value
    assert decision.audit_metadata["categories"] == ["aadhaar", "email", "phone"]


async def test_academic_integrity_policy_converts_direct_answer_to_tutor_mode() -> None:
    decision = await AcademicIntegrityPolicy().evaluate(
        "input", "Give me the final answer to my graded assignment", {}
    )

    assert decision.action == PolicyAction.TRANSFORM
    assert decision.reason_code == "academic_integrity.tutor_mode"
    assert "TUTOR MODE" in decision.value


async def test_equity_policy_reframes_group_ability_claim() -> None:
    decision = await EducationalEquityPolicy().evaluate(
        "input", "Are rural students less capable at AI?", {}
    )

    assert decision.action == PolicyAction.TRANSFORM
    assert decision.reason_code == "educational_equity.reframed"
    assert "structural conditions" in decision.value


async def test_composite_policy_registers_and_reports_domain_guardrail() -> None:
    engine = CompositePolicyEngine()
    engine.register(AcademicIntegrityPolicy())
    decision = await engine.evaluate(
        "input",
        "Give me the answer to this graded assignment",
        {"agent": _agent()},
    )

    assert decision.action == PolicyAction.TRANSFORM
    assert "academic_integrity" in decision.audit_metadata["policies_triggered"]


async def test_runtime_guardrail_emits_traccia_detection_attributes() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    engine = CompositePolicyEngine((AcademicIntegrityPolicy(),))
    engine._tracer = provider.get_tracer("guardrail-test")

    await engine.evaluate(
        "input",
        "Give me the final answer to my graded assignment",
        {"agent": _agent()},
    )

    span = exporter.get_finished_spans()[0]
    assert span.attributes["span.type"] == "guardrail"
    assert span.attributes["guardrail.name"] == "academic_integrity"
    assert span.attributes["guardrail.category"] == "input_validation"
    assert span.attributes["guardrail.triggered"] is True
    assert span.attributes["guardrail.enforcement_mode"] == "warn"

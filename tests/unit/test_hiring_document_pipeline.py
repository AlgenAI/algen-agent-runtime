from algen_agent_runtime.security.documents import redact_pii
from examples.case_study_responsible_hiring.application.document_pipeline import (
    candidate_identity_entities,
)


def test_full_resume_header_is_redacted_when_form_name_is_abbreviated() -> None:
    resume = "Abdul Majeed\nSUMMARY\nBuilt Python services."
    entities = candidate_identity_entities("Abdul", resume)

    sanitized = redact_pii(resume, entities=entities)

    assert "Abdul" not in sanitized
    assert "Majeed" not in sanitized


def test_section_heading_is_not_treated_as_a_candidate_name() -> None:
    assert candidate_identity_entities("Abdul", "SUMMARY\nBuilt Python services.") == ("Abdul",)

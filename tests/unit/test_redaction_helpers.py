"""redact_pii and redact_secrets apply the guardrails' own rules to a host's values."""

from __future__ import annotations

import pytest

from algen_agent_runtime.policies.engine import PIIRedactionPolicy, SecretRedactionPolicy
from algen_agent_runtime.security import PII_ENTITIES, redact_pii, redact_secrets

AADHAAR = "2345 6789 0124"  # passes the Verhoeff check
NOT_AADHAAR = "2345 6789 0123"


def test_redact_pii_uses_the_guardrails_placeholders() -> None:
    text = f"Mail a@b.co, card 4111 1111 1111 1111, Aadhaar {AADHAAR}, call 98765 43210"
    assert redact_pii(text) == (
        "Mail [REDACTED_EMAIL], card [REDACTED_CARD], Aadhaar [REDACTED_AADHAAR], "
        "call [REDACTED_PHONE]"
    )
    assert redact_pii(f"id {NOT_AADHAAR}") == f"id {NOT_AADHAAR}"
    assert redact_pii("order 9876543210") == "order 9876543210"


def test_redact_pii_walks_dicts_and_lists_and_reads_field_names() -> None:
    value = {
        "mobile_no": "9876543210",
        "contactPhone": ["9876543210"],
        "order_id": "9876543210",
        "notes": ("reach me at a@b.co", 7, None),
    }
    assert redact_pii(value) == {
        "mobile_no": "[REDACTED_PHONE]",
        "contactPhone": ["[REDACTED_PHONE]"],
        "order_id": "9876543210",
        "notes": ("reach me at [REDACTED_EMAIL]", 7, None),
    }
    assert value["mobile_no"] == "9876543210"  # the input is not modified


def test_redact_pii_entities_and_exempt() -> None:
    text = "a@b.co phone 9876543210"
    assert redact_pii(text, entities=["email"]) == "[REDACTED_EMAIL] phone 9876543210"
    assert redact_pii(text, exempt=["email"]) == "a@b.co phone [REDACTED_PHONE]"
    assert redact_pii(text, entities=["email"], exempt=["email"]) == text
    with pytest.raises(ValueError, match="unknown PII kind"):
        redact_pii(text, entities=["ssn"])
    assert set(PII_ENTITIES) == {"email", "phone", "aadhaar", "card"}


def test_redact_pii_honours_schema_identifier_and_email_fields() -> None:
    schema = {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string", "format": "identifier"},
            "to": {"type": "string", "format": "email"},
            "body": {"type": "string"},
        },
    }
    value = {"chat_id": "9876543210", "to": "a@b.co", "body": "a@b.co"}
    assert redact_pii(value, schema=schema) == {
        "chat_id": "9876543210",
        "to": "a@b.co",
        "body": "[REDACTED_EMAIL]",
    }
    assert redact_pii(value)["to"] == "[REDACTED_EMAIL]"


def test_redact_pii_matches_the_policy() -> None:
    value = {"phone": "9876543210", "text": f"Aadhaar {AADHAAR} and a@b.co"}
    assert redact_pii(value) == PIIRedactionPolicy().redact_value(value)


def test_redact_secrets_masks_credentials_in_text_and_by_key() -> None:
    value = {
        "password": "hunter2",
        "db.password": {"nested": "value"},
        "githubToken": "abc",
        "Authorization": "Bearer abc.def.123",
        "max_tokens": 512,
        "log": "api_key" + "=abcdefgh1234 then Bearer eyJhbGciOi.payload.sig",
        "items": ["SECRET: s3cr3tvalue", "The secret: consistency"],
    }
    assert redact_secrets(value) == {
        "password": "[REDACTED]",
        "db.password": "[REDACTED]",
        "githubToken": "[REDACTED]",
        "Authorization": "[REDACTED]",
        "max_tokens": 512,
        "log": "api_key=[REDACTED] then Bearer [REDACTED]",
        "items": ["SECRET: [REDACTED]", "The secret: consistency"],
    }
    text = 'config: {"token": "Zx81kdLq0w"}'
    assert redact_secrets(text) == SecretRedactionPolicy._redact_text(text)[0]

"""A repair after failed verification ends with a user turn that says what failed (#90)."""

from __future__ import annotations

import json
from typing import Any

import httpx
from conftest import make_agent, make_runtime

from algen_agent_runtime.context.packing import REPAIR_MESSAGE_NAME
from algen_agent_runtime.exceptions.errors import ProviderError
from algen_agent_runtime.models.providers.anthropic import AnthropicProvider
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.types.contracts import (
    ErrorKind,
    MemoryPolicy,
    Message,
    ModelRequest,
    ModelResponse,
    RequestOverrides,
    Role,
    RunRequest,
)
from algen_agent_runtime.verification.verifiers import JsonSchemaVerifier, schema_findings

SCHEMA = {
    "type": "object",
    "properties": {
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                    "score": {"type": "integer", "minimum": 0, "maximum": 4},
                },
                "required": ["evidence", "score"],
            },
        }
    },
    "required": ["criteria"],
}
PRIVATE = "ana@contoso.com"
TOO_MANY = {"criteria": [{"evidence": [PRIVATE, "b", "c", "d"], "score": 9}]}
FIXED = {"criteria": [{"evidence": ["a"], "score": 3}]}


class NoPrefillProvider(MockModelProvider):
    """A mock that answers like a model refusing assistant prefill (Claude 5.x)."""

    def __init__(self, *replies: Any) -> None:
        super().__init__([json.dumps(reply) for reply in replies])

    async def generate(self, request: ModelRequest) -> ModelResponse:
        turns = [message for message in request.messages if message.role != Role.SYSTEM]
        if turns and turns[-1].role == Role.ASSISTANT:
            raise ProviderError(
                "This model does not support assistant message prefill.",
                ErrorKind.INVALID_REQUEST,
                status_code=400,
            )
        return await super().generate(request)


def test_findings_name_paths_and_limits_never_values() -> None:
    findings = schema_findings(TOO_MANY, SCHEMA)

    assert findings == (
        "criteria[0].evidence: 4 items, at most 3",
        "criteria[0].score: must be at most 4",
    )
    assert schema_findings({"criteria": [{"score": "x"}]}, SCHEMA) == (
        "criteria[0]: missing required evidence",
        "criteria[0].score: must be of type integer",
    )


async def test_the_json_schema_verifier_reports_findings_without_values() -> None:
    result = await JsonSchemaVerifier().verify(json.dumps(TOO_MANY), {"schema": SCHEMA})
    assert not result.passed and len(result.findings) == 2
    assert PRIVATE not in result.message

    broken = await JsonSchemaVerifier().verify('{"criteria": [', {"schema": SCHEMA})
    assert broken.findings[0].startswith("not valid JSON:")


async def test_a_model_that_refuses_prefill_recovers_from_one_failed_verification() -> None:
    provider = NoPrefillProvider(TOO_MANY, FIXED)
    runtime = make_runtime(
        provider, make_agent(memory_policy=MemoryPolicy(store_tool_payloads=True))
    )

    result = await runtime.run(
        RunRequest(
            agent="test-agent",
            input="Grade it.",
            tenant_id="t",
            user_id="u",
            session_id="s",
            overrides=RequestOverrides(response_schema=SCHEMA),
        )
    )

    assert result.status == "completed", result.error
    assert json.loads(result.output or "") == FIXED
    repair = provider.requests[1].messages[-1]
    assert repair.role == Role.USER and repair.name == REPAIR_MESSAGE_NAME
    assert "- criteria[0].evidence: 4 items, at most 3" in repair.text_content
    assert PRIVATE not in repair.text_content
    # The repair turn is the Runtime's, not the user's: it is not kept in session memory.
    remembered = await runtime.memory.get("t", "s", 50)
    assert [message.role for message in remembered] == [Role.USER, Role.ASSISTANT]
    assert all(message.name != REPAIR_MESSAGE_NAME for message in remembered)


async def test_the_anthropic_adapter_never_ends_with_an_assistant_turn() -> None:
    sent: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "m",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    class _Secrets:
        async def get(self, reference: str) -> str:
            return "test-key"

    provider = AnthropicProvider(
        "env://ANTHROPIC_API_KEY",
        "claude-sonnet-5-5",
        secret_provider=_Secrets(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    await provider.generate(
        ModelRequest(
            messages=(
                Message.text(Role.USER, "q"),
                Message.text(Role.ASSISTANT, "an earlier answer"),
                Message.text(Role.SYSTEM, "a late instruction"),
            )
        )
    )

    assert sent[0]["messages"][-1] == {
        "role": "user",
        "content": [{"type": "text", "text": "Continue."}],
    }

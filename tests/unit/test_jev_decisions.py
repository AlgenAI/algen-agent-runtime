from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from algen_agent_runtime.decisions import (
    JevDecisionClient,
    JevQuestion,
    JevRequest,
    JevSettings,
)


def test_jev_questions_enforce_bounded_shapes() -> None:
    choice = JevQuestion(
        type="choice",
        instructions="Route this HR request",
        criteria={"read": "Safe lookup", "write": "State change"},
    )
    assert choice.type == "choice"
    with pytest.raises(ValidationError):
        JevQuestion(type="choice", instructions="Invalid", criteria={"only": "one"})
    with pytest.raises(ValidationError):
        JevQuestion(type="noul", instructions="Invalid", criteria={"yes": "yes"})


@pytest.mark.asyncio
async def test_jev_client_returns_typed_decisions_without_leaking_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-value"
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    "route": {
                        "type": "choice",
                        "choice": "read",
                        "confidence": 0.91,
                        "probabilities": {"read": 0.91, "write": 0.09},
                    }
                },
                "usage": {"input_tokens": 42, "output_tokens": 0},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    decision = JevDecisionClient(
        JevSettings(api_key=SecretStr("secret-value")), client=client
    )
    result = await decision.decide(
        JevRequest(
            state="Show current candidates",
            questions={
                "route": JevQuestion(
                    type="choice",
                    instructions="Choose the safest route",
                    criteria={"read": "Lookup", "write": "Mutation"},
                )
            },
        )
    )
    assert result.answers["route"].choice == "read"
    assert result.answers["route"].decision_confidence == 0.91
    assert "secret-value" not in repr(result)
    await client.aclose()

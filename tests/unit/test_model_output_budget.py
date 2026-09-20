from __future__ import annotations

from conftest import make_agent, make_runtime

from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.types.contracts import (
    Budget,
    FinishReason,
    Message,
    ModelResponse,
    RequestOverrides,
    Role,
    RunRequest,
    TokenUsage,
)


async def test_runtime_uses_separate_per_call_output_cap() -> None:
    provider = MockModelProvider(["answer"])
    runtime = make_runtime(
        provider,
        make_agent(
            budget=Budget(
                max_tokens=24_000,
                max_output_tokens=5_000,
                max_cost_usd=1,
                max_latency_seconds=30,
            )
        ),
    )

    result = await runtime.run(
        RunRequest(agent="test-agent", input="hello", tenant_id="t", user_id="u")
    )

    assert result.error is None
    assert provider.requests[0].max_output_tokens == 5_000


async def test_runtime_retries_and_charges_truncated_structured_completion() -> None:
    truncated = ModelResponse(
        message=Message.text(Role.ASSISTANT, ""),
        finish_reason=FinishReason.LENGTH,
        usage=TokenUsage(input_tokens=100, output_tokens=500),
        model="deterministic",
        provider="mock",
    )
    provider = MockModelProvider([truncated, "{}"])
    runtime = make_runtime(provider)

    result = await runtime.run(
        RunRequest(
            agent="test-agent",
            input="return an object",
            tenant_id="t",
            user_id="u",
            overrides=RequestOverrides(
                response_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                }
            ),
        )
    )

    assert result.output == "{}"
    assert len(provider.requests) == 2
    assert result.execution_summary.model_calls == 2
    assert result.execution_summary.retries == 1
    assert result.execution_summary.usage.total_tokens >= 611
    assert provider.requests[1].max_output_tokens == 16_384


async def test_runtime_increases_small_output_cap_after_truncation() -> None:
    truncated = ModelResponse(
        message=Message.text(Role.ASSISTANT, "partial"),
        finish_reason=FinishReason.LENGTH,
        usage=TokenUsage(input_tokens=100, output_tokens=700),
        model="deterministic",
        provider="mock",
    )
    provider = MockModelProvider([truncated, "complete"])
    runtime = make_runtime(
        provider,
        make_agent(
            budget=Budget(
                max_tokens=12_000,
                max_output_tokens=1_800,
                max_cost_usd=1,
                max_latency_seconds=30,
            )
        ),
    )

    result = await runtime.run(
        RunRequest(agent="test-agent", input="hello", tenant_id="t", user_id="u")
    )

    assert result.output == "complete"
    assert provider.requests[0].max_output_tokens == 1_800
    assert provider.requests[1].max_output_tokens == 3_600


async def test_runtime_repairs_malformed_schema_constrained_output() -> None:
    provider = MockModelProvider(["not-json", "{}"])
    runtime = make_runtime(provider)

    result = await runtime.run(
        RunRequest(
            agent="test-agent",
            input="return an object",
            tenant_id="t",
            user_id="u",
            overrides=RequestOverrides(
                response_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                }
            ),
        )
    )

    assert result.output == "{}"
    assert len(provider.requests) == 2
    assert "Repair the previous response" in provider.requests[1].messages[-1].text_content

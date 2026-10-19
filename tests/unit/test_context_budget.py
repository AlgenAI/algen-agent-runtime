from __future__ import annotations

import pytest
from conftest import make_agent, make_runtime
from pydantic import ValidationError

from algen_agent_runtime.context.budget import estimate_request_tokens, prepare_model_request
from algen_agent_runtime.exceptions.errors import (
    BudgetExceededError,
    ConfigurationError,
    ProviderError,
)
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.types.contracts import (
    Budget,
    ErrorKind,
    FinishReason,
    ImageBlock,
    Message,
    ModelProfile,
    ModelRequest,
    ModelResponse,
    Role,
    RunRequest,
    TokenUsage,
    ToolCall,
    ToolSpec,
)


def profile(name: str, window: int | None = 600, quality: int = 1) -> ModelProfile:
    return ModelProfile(
        name=name,
        provider=name,
        model="test",
        quality_tier=quality,
        context_window_tokens=window,
        context_safety_margin_tokens=32,
    )


def request() -> ModelRequest:
    return ModelRequest(
        messages=(
            Message.text(Role.SYSTEM, "Keep these instructions."),
            Message.text(Role.USER, "old task"),
            Message.text(Role.ASSISTANT, "old history " * 400),
            Message.text(Role.USER, "current task"),
        ),
        max_output_tokens=100,
    )


def fail_provider(request: ModelRequest) -> ModelResponse:
    raise ProviderError("temporarily unavailable", ErrorKind.UNAVAILABLE, retryable=True)


@pytest.mark.parametrize("stream", [False, True])
async def test_router_packs_per_model_without_mutating_original_request(stream: bool) -> None:
    router = ModelRouter()
    provider = MockModelProvider(["answer"])
    router.register_provider(provider, registration_id="small")
    original = request()
    original_json = original.model_dump_json()
    selected = profile("small")

    if stream:
        events = [event async for event in router.stream(original, [selected])]
        assert events[-1].response is not None
    else:
        await router.generate(original, [selected])

    sent = provider.requests[0]
    assert sent.messages == (original.messages[0], original.messages[-1])
    assert sent.max_output_tokens == 100
    assert estimate_request_tokens(sent) + 100 + 32 <= 600
    assert original.model_dump_json() == original_json


@pytest.mark.parametrize("primary_window,fallback_window", [(600, 5000), (5000, 600)])
async def test_each_fallback_is_prepared_from_original_history(
    primary_window: int, fallback_window: int
) -> None:
    router = ModelRouter()
    small = MockModelProvider(handler=fail_provider)
    large = MockModelProvider(["answer"])
    router.register_provider(small, registration_id="small")
    router.register_provider(large, registration_id="large")
    original = request()

    await router.generate(
        original, [profile("small", primary_window, 2), profile("large", fallback_window)]
    )

    assert len(small.requests[0].messages) == (2 if primary_window == 600 else 4)
    assert len(large.requests[0].messages) == (2 if fallback_window == 600 else 4)
    assert len(original.messages) == 4


@pytest.mark.parametrize("stream", [False, True])
async def test_unfit_candidate_is_skipped_without_provider_failure_or_rate_limit_use(
    stream: bool,
) -> None:
    router = ModelRouter(circuit_failure_threshold=1)
    tiny = MockModelProvider(["must not run"])
    fallback = MockModelProvider(["answer"])
    router.register_provider(tiny, registration_id="tiny")
    router.register_provider(fallback, registration_id="fallback")
    profiles = [profile("tiny", 50, 2), profile("fallback")]
    if stream:
        _ = [event async for event in router.stream(request(), profiles)]
    else:
        await router.generate(request(), profiles)

    assert not tiny.requests
    assert len(fallback.requests) == 1
    assert router._health["tiny"].consecutive_failures == 0
    assert not router._limits["tiny"]._timestamps


@pytest.mark.parametrize("stream", [False, True])
async def test_no_candidate_fits_raises_budget_error_before_dispatch(stream: bool) -> None:
    router = ModelRouter()
    provider = MockModelProvider()
    router.register_provider(provider, registration_id="tiny")
    with pytest.raises(BudgetExceededError, match="context budget"):
        if stream:
            _ = [event async for event in router.stream(request(), [profile("tiny", 50)])]
        else:
            await router.generate(request(), [profile("tiny", 50)])
    assert not provider.requests


@pytest.mark.parametrize("field", ["tools", "response_schema", "extensions"])
def test_request_metadata_consumes_context_space(field: str) -> None:
    original = request()
    selected = profile("small")
    prepare_model_request(original, selected)
    if field == "tools":
        original = original.model_copy(
            update={
                "tools": (
                    ToolSpec(
                        name="lookup",
                        description="long description " * 500,
                        input_schema={"type": "object"},
                    ),
                )
            }
        )
    elif field == "response_schema":
        original = original.model_copy(
            update={
                "response_schema": {
                    "type": "string",
                    "description": "schema description " * 500,
                }
            }
        )
    else:
        selected = selected.model_copy(update={"extensions": {"extra_context": "x" * 4000}})
    with pytest.raises(BudgetExceededError):
        prepare_model_request(original, selected)


def test_latest_tool_exchange_remains_atomic_under_model_budget() -> None:
    original = request()
    call = Message(role=Role.ASSISTANT, content=(), tool_calls=(ToolCall(id="a", name="lookup"),))
    result = Message(role=Role.TOOL, content=(), tool_call_id="a")
    original = original.model_copy(update={"messages": (*original.messages, call, result)})
    packed = prepare_model_request(original, profile("small"))
    assert packed.messages == (original.messages[0], original.messages[3], call, result)


def test_exact_budget_boundary_and_safety_margin() -> None:
    original = request().model_copy(update={"messages": (Message.text(Role.USER, "task"),)})
    window = estimate_request_tokens(original) + 100 + 32
    assert prepare_model_request(original, profile("small", window)).messages == original.messages
    with pytest.raises(BudgetExceededError):
        prepare_model_request(original, profile("small", window - 1))


def test_legacy_profile_preserves_unbounded_request_behavior() -> None:
    original = request().model_copy(update={"max_output_tokens": None})
    assert prepare_model_request(original, profile("legacy", None)).messages == original.messages


def test_configured_limit_requires_explicit_output_reservation() -> None:
    with pytest.raises(ConfigurationError, match="max_output_tokens"):
        prepare_model_request(
            request().model_copy(update={"max_output_tokens": None}), profile("small")
        )


@pytest.mark.parametrize("override", ["messages", "max_tokens", "max_completion_tokens", "model"])
def test_extensions_cannot_bypass_context_limits(override: str) -> None:
    selected = profile("small").model_copy(update={"extensions": {override: "override"}})
    with pytest.raises(ConfigurationError, match="extensions"):
        prepare_model_request(request(), selected)


def test_multimodal_input_is_not_silently_counted_as_a_short_url() -> None:
    original = request().model_copy(
        update={
            "messages": (
                Message(
                    role=Role.USER, content=(ImageBlock(url="https://example.invalid/image.png"),)
                ),
            )
        }
    )
    with pytest.raises(ConfigurationError, match="image/file token costs"):
        prepare_model_request(original, profile("small"))


@pytest.mark.parametrize(
    "fields", [{"context_window_tokens": 0}, {"context_safety_margin_tokens": -1}]
)
def test_invalid_profile_limits_are_rejected(fields: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        ModelProfile(name="invalid", **fields)


async def test_output_limit_recovery_rechecks_context_before_retry_dispatch() -> None:
    provider = MockModelProvider(
        [
            ModelResponse(
                message=Message.text(Role.ASSISTANT, "partial"),
                finish_reason=FinishReason.LENGTH,
                model="test",
                provider="mock",
                usage=TokenUsage(input_tokens=20, output_tokens=100),
            ),
            "must not run",
        ]
    )
    runtime = make_runtime(
        provider,
        make_agent(
            default_model=profile("mock", 300),
            budget=Budget(max_output_tokens=100),
        ),
    )
    result = await runtime.run(
        RunRequest(agent="test-agent", input="task", tenant_id="t", user_id="u")
    )
    assert result.status == "failed"
    assert len(provider.requests) == 1
    assert result.execution_summary.usage.total_tokens == 120


async def test_stream_does_not_fall_back_after_provider_dispatch() -> None:
    router = ModelRouter()
    primary = MockModelProvider(handler=fail_provider)
    fallback = MockModelProvider(["must not run"])
    router.register_provider(primary, registration_id="primary")
    router.register_provider(fallback, registration_id="fallback")
    with pytest.raises(ProviderError):
        _ = [
            event
            async for event in router.stream(
                request(), [profile("primary", 600, 2), profile("fallback")]
            )
        ]
    assert not fallback.requests


async def test_provider_failure_is_preserved_when_another_candidate_exceeds_budget() -> None:
    router = ModelRouter()
    failing = MockModelProvider(handler=fail_provider)
    tiny = MockModelProvider()
    router.register_provider(failing, registration_id="failing")
    router.register_provider(tiny, registration_id="tiny")
    with pytest.raises(ProviderError) as error:
        await router.generate(request(), [profile("failing", 600, 2), profile("tiny", 50)])
    assert error.value.error_kind == ErrorKind.UNAVAILABLE
    assert error.value.retryable
    assert len(failing.requests) == 1
    assert not tiny.requests

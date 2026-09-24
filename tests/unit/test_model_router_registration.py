from __future__ import annotations

from collections.abc import Sequence

import pytest

from algen_agent_runtime.cache import CacheService
from algen_agent_runtime.exceptions.errors import CapabilityError
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.types.contracts import (
    Message,
    ModelCapabilities,
    ModelProfile,
    ModelRequest,
    ModelResponse,
    Role,
)


class CustomCapabilityMockProvider(MockModelProvider):
    def __init__(
        self,
        responses: Sequence[str | ModelResponse] = (),
        caps: ModelCapabilities | None = None,
    ) -> None:
        super().__init__(responses)
        self._caps = caps or ModelCapabilities(
            chat=True,
            streaming=True,
            tools=True,
            structured_output=True,
            images=False,
        )

    async def capabilities(self, model: str) -> ModelCapabilities:
        return self._caps


@pytest.mark.asyncio
async def test_two_same_type_providers_independently_addressable() -> None:
    router = ModelRouter()
    provider_a = MockModelProvider(["from-a"])
    provider_b = MockModelProvider(["from-b"])

    router.register_provider(provider_a, registration_id="mock-a")
    router.register_provider(provider_b, registration_id="mock-b")

    assert router.provider("mock-a") is provider_a
    assert router.provider("mock-b") is provider_b

    with pytest.raises(CapabilityError, match="not registered"):
        router.provider("mock")


@pytest.mark.asyncio
async def test_different_limits_costs_locality_per_instance() -> None:
    router = ModelRouter()
    local_provider = MockModelProvider(["local response"])
    cloud_provider = MockModelProvider(["cloud response"])

    router.register_provider(
        local_provider,
        cost_per_1k_input=1.0,
        cost_per_1k_output=1.0,
        is_local=True,
        registration_id="local-inst",
    )
    router.register_provider(
        cloud_provider,
        cost_per_1k_input=10.0,
        cost_per_1k_output=10.0,
        is_local=False,
        registration_id="cloud-inst",
    )

    profiles = (
        ModelProfile(
            name="cloud",
            provider="cloud-inst",
            model="test-model",
            routing_preference="local_first",
        ),
        ModelProfile(
            name="local",
            provider="local-inst",
            model="test-model",
            routing_preference="local_first",
        ),
    )

    candidates = await router.candidates(profiles)
    assert len(candidates) == 2
    # Local candidate must be ranked first under local_first preference
    assert candidates[0][0] is local_provider
    assert candidates[1][0] is cloud_provider

    # Verify cost calculation reflects the chosen provider instance
    req = ModelRequest(messages=(Message.text(Role.USER, "hello"),))
    res = await router.generate(req, (profiles[1],))  # local: $1 per 1k tokens
    assert res.usage.estimated_cost_usd is not None
    # MockModelProvider produces input_tokens=10, output_tokens=3 ("local response" len=14, 14//4=3)
    # 10 * 1 + 3 * 1 = 13 / 1000 = $0.013
    assert pytest.approx(res.usage.estimated_cost_usd, rel=1e-3) == 0.013

    res_cloud = await router.generate(req, (profiles[0],))  # cloud: $10 per 1k tokens
    # 10 * 10 + 3 * 10 = 130 / 1000 = $0.13
    assert pytest.approx(res_cloud.usage.estimated_cost_usd, rel=1e-3) == 0.13


def test_duplicate_registration_id_raises_value_error() -> None:
    router = ModelRouter()
    provider1 = MockModelProvider()
    provider2 = MockModelProvider()

    router.register_provider(provider1, registration_id="shared-id")
    with pytest.raises(ValueError, match="already registered"):
        router.register_provider(provider2, registration_id="shared-id")


def test_backward_compatible_registration_without_explicit_id() -> None:
    router = ModelRouter()
    provider = MockModelProvider()
    # Omitting registration_id defaults to provider.provider_id ("mock")
    router.register_provider(provider)
    assert router.provider("mock") is provider

    # Registering a second provider without registration_id attempts "mock" again and fails
    with pytest.raises(ValueError, match="already registered"):
        router.register_provider(MockModelProvider())


@pytest.mark.asyncio
async def test_capability_cache_isolation_between_instances() -> None:
    cache = CacheService("memory")
    router = ModelRouter(cache=cache)

    provider_a = CustomCapabilityMockProvider(
        caps=ModelCapabilities(chat=True, streaming=True, tools=True)
    )
    provider_b = CustomCapabilityMockProvider(
        caps=ModelCapabilities(chat=True, streaming=False, tools=False)
    )

    router.register_provider(provider_a, registration_id="inst-a")
    router.register_provider(provider_b, registration_id="inst-b")

    candidates_a = await router.candidates(
        (
            ModelProfile(
                name="a",
                provider="inst-a",
                model="m1",
                required_capabilities=frozenset({"streaming", "tools"}),
            ),
        )
    )
    assert len(candidates_a) == 1

    candidates_b = await router.candidates(
        (
            ModelProfile(
                name="b",
                provider="inst-b",
                model="m1",
                required_capabilities=frozenset({"streaming"}),
            ),
        )
    )
    # inst-b does not support streaming, so candidates must be empty
    assert len(candidates_b) == 0


@pytest.mark.asyncio
async def test_capability_narrowing_omitted_preserves_adapter_defaults() -> None:
    router = ModelRouter()
    provider = CustomCapabilityMockProvider(
        caps=ModelCapabilities(chat=True, streaming=True, tools=True)
    )
    # capability_override=None preserves adapter discovery
    router.register_provider(provider, registration_id="full", capability_override=None)

    candidates = await router.candidates(
        (
            ModelProfile(
                name="prof",
                provider="full",
                model="m",
                required_capabilities=frozenset({"streaming", "tools"}),
            ),
        )
    )
    assert len(candidates) == 1


@pytest.mark.asyncio
async def test_capability_narrowing_explicit_policy_narrows_support() -> None:
    router = ModelRouter()
    provider = CustomCapabilityMockProvider(
        caps=ModelCapabilities(chat=True, streaming=True, tools=True)
    )
    # Explicitly narrow streaming to False
    narrowed = ModelCapabilities(chat=True, streaming=False, tools=True)
    router.register_provider(provider, registration_id="narrowed", capability_override=narrowed)

    # Requires tools (still True) -> candidates present
    cand_tools = await router.candidates(
        (
            ModelProfile(
                name="prof-tools",
                provider="narrowed",
                model="m",
                required_capabilities=frozenset({"tools"}),
            ),
        )
    )
    assert len(cand_tools) == 1

    # Requires streaming (narrowed to False) -> candidate excluded
    cand_streaming = await router.candidates(
        (
            ModelProfile(
                name="prof-streaming",
                provider="narrowed",
                model="m",
                required_capabilities=frozenset({"streaming"}),
            ),
        )
    )
    assert len(cand_streaming) == 0


@pytest.mark.asyncio
async def test_capability_widening_is_prevented(caplog: pytest.LogCaptureFixture) -> None:
    router = ModelRouter()
    # Adapter does NOT support images
    provider = CustomCapabilityMockProvider(caps=ModelCapabilities(chat=True, images=False))
    # Config attempts to widen images=True
    attempted_widening = ModelCapabilities(chat=True, images=True)
    router.register_provider(
        provider, registration_id="widened", capability_override=attempted_widening
    )

    # Candidate requiring images must be excluded
    cand = await router.candidates(
        (
            ModelProfile(
                name="prof-images",
                provider="widened",
                model="m",
                required_capabilities=frozenset({"images"}),
            ),
        )
    )
    assert len(cand) == 0


@pytest.mark.asyncio
async def test_partial_capability_override_preserves_unspecified_adapter_capabilities() -> None:
    router = ModelRouter()
    provider = CustomCapabilityMockProvider(
        caps=ModelCapabilities(
            chat=True,
            streaming=True,
            tools=True,
            structured_output=True,
            json_schema=True,
            files=True,
        )
    )
    router.register_provider(
        provider,
        registration_id="partial-test",
        capability_override=ModelCapabilities(tools=False),
    )

    # Required: streaming, structured_output, json_schema, files remain True
    for cap in ("chat", "streaming", "structured_output", "json_schema", "files"):
        cands = await router.candidates(
            (
                ModelProfile(
                    name=f"prof-{cap}",
                    provider="partial-test",
                    model="m",
                    required_capabilities=frozenset({cap}),
                ),
            )
        )
        assert len(cands) == 1, f"Expected {cap} to remain available"

    # Required: tools is False
    cands_tools = await router.candidates(
        (
            ModelProfile(
                name="prof-tools",
                provider="partial-test",
                model="m",
                required_capabilities=frozenset({"tools"}),
            ),
        )
    )
    assert len(cands_tools) == 0


@pytest.mark.asyncio
async def test_empty_capability_override_preserves_adapter_capabilities() -> None:
    router = ModelRouter()
    provider = CustomCapabilityMockProvider(
        caps=ModelCapabilities(
            chat=True,
            streaming=True,
            tools=True,
            structured_output=True,
            json_schema=True,
            files=True,
        )
    )
    router.register_provider(
        provider,
        registration_id="empty-test",
        capability_override=ModelCapabilities(),
    )

    for cap in ("chat", "streaming", "tools", "structured_output", "json_schema", "files"):
        cands = await router.candidates(
            (
                ModelProfile(
                    name=f"prof-{cap}",
                    provider="empty-test",
                    model="m",
                    required_capabilities=frozenset({cap}),
                ),
            )
        )
        assert len(cands) == 1, f"Expected {cap} to be preserved with empty ModelCapabilities()"


@pytest.mark.asyncio
async def test_partial_capability_widening_does_not_disable_other_fields() -> None:
    router = ModelRouter()
    provider = CustomCapabilityMockProvider(
        caps=ModelCapabilities(chat=True, streaming=True, images=False)
    )
    # Attempt widening images=True
    router.register_provider(
        provider,
        registration_id="widen-test",
        capability_override=ModelCapabilities(images=True),
    )

    # images remains False
    cands_img = await router.candidates(
        (
            ModelProfile(
                name="prof-img",
                provider="widen-test",
                model="m",
                required_capabilities=frozenset({"images"}),
            ),
        )
    )
    assert len(cands_img) == 0

    # chat and streaming remain True
    for cap in ("chat", "streaming"):
        cands = await router.candidates(
            (
                ModelProfile(
                    name=f"prof-{cap}",
                    provider="widen-test",
                    model="m",
                    required_capabilities=frozenset({cap}),
                ),
            )
        )
        assert len(cands) == 1, f"Expected {cap} to remain True when images widening is attempted"


@pytest.mark.asyncio
async def test_partial_capability_override_isolated_in_cache() -> None:
    cache = CacheService("memory")
    router = ModelRouter(cache=cache)

    provider_a = CustomCapabilityMockProvider(
        caps=ModelCapabilities(chat=True, streaming=True, tools=True, files=True)
    )
    provider_b = CustomCapabilityMockProvider(
        caps=ModelCapabilities(chat=True, streaming=True, tools=True, files=True)
    )

    # inst-a narrows tools=False, preserves streaming & files
    router.register_provider(
        provider_a,
        registration_id="inst-a",
        capability_override=ModelCapabilities(tools=False),
    )
    # inst-b narrows files=False, preserves streaming & tools
    router.register_provider(
        provider_b,
        registration_id="inst-b",
        capability_override=ModelCapabilities(files=False),
    )

    # First call primes cache
    cands_a_tools = await router.candidates(
        (
            ModelProfile(
                name="a-tools",
                provider="inst-a",
                model="m",
                required_capabilities=frozenset({"tools"}),
            ),
        )
    )
    assert len(cands_a_tools) == 0

    cands_b_tools = await router.candidates(
        (
            ModelProfile(
                name="b-tools",
                provider="inst-b",
                model="m",
                required_capabilities=frozenset({"tools"}),
            ),
        )
    )
    assert len(cands_b_tools) == 1

    cands_a_files = await router.candidates(
        (
            ModelProfile(
                name="a-files",
                provider="inst-a",
                model="m",
                required_capabilities=frozenset({"files"}),
            ),
        )
    )
    assert len(cands_a_files) == 1

    cands_b_files = await router.candidates(
        (
            ModelProfile(
                name="b-files",
                provider="inst-b",
                model="m",
                required_capabilities=frozenset({"files"}),
            ),
        )
    )
    assert len(cands_b_files) == 0

import pytest

from algen_agent_runtime.exceptions.errors import ProviderError
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.types.contracts import ErrorKind, Message, ModelProfile, ModelRequest, Role


class FailingProvider(MockModelProvider):
    provider_id = "failing"

    async def generate(self, request):
        raise ProviderError("down", ErrorKind.UNAVAILABLE, True)


async def test_router_falls_back_after_provider_failure() -> None:
    router = ModelRouter()
    router.register_provider(FailingProvider())
    router.register_provider(MockModelProvider(["fallback"]))
    response = await router.generate(
        ModelRequest(messages=(Message.text(Role.USER, "x"),)),
        (
            ModelProfile(name="first", provider="failing", model="x", quality_tier=2),
            ModelProfile(name="second", provider="mock", model="x", quality_tier=1),
        ),
    )
    assert response.message.text_content == "fallback"


class ForbiddenProvider(MockModelProvider):
    provider_id = "forbidden"

    async def generate(self, request):
        raise ProviderError("access denied", ErrorKind.AUTHORIZATION, False)


async def test_router_preserves_single_provider_error_classification() -> None:
    router = ModelRouter()
    router.register_provider(ForbiddenProvider())

    with pytest.raises(ProviderError) as captured:
        await router.generate(
            ModelRequest(messages=(Message.text(Role.USER, "x"),)),
            (ModelProfile(name="only", provider="forbidden", model="x"),),
        )

    assert captured.value.error_kind == ErrorKind.AUTHORIZATION
    assert captured.value.retryable is False


async def test_router_falls_back_between_same_adapter_instances() -> None:
    router = ModelRouter()
    # Both providers share the same underlying adapter type / provider_id ("mock")
    failing = FailingProvider()
    backup = MockModelProvider(["from-backup"])

    router.register_provider(failing, registration_id="mock-primary")
    router.register_provider(backup, registration_id="mock-backup")

    response = await router.generate(
        ModelRequest(messages=(Message.text(Role.USER, "hello"),)),
        (
            ModelProfile(name="first", provider="mock-primary", model="x", quality_tier=2),
            ModelProfile(name="second", provider="mock-backup", model="x", quality_tier=1),
        ),
    )
    assert response.message.text_content == "from-backup"


async def test_same_type_circuit_breaker_isolation() -> None:
    router = ModelRouter(circuit_failure_threshold=2, circuit_reset_seconds=60)
    failing = FailingProvider()
    backup = MockModelProvider(["ok"])

    router.register_provider(failing, registration_id="inst-1")
    router.register_provider(backup, registration_id="inst-2")

    profiles = (
        ModelProfile(name="p1", provider="inst-1", model="x", quality_tier=2),
        ModelProfile(name="p2", provider="inst-2", model="x", quality_tier=1),
    )

    # Trigger 2 failures on inst-1 to trip its circuit breaker
    for _ in range(2):
        await router.generate(ModelRequest(messages=(Message.text(Role.USER, "x"),)), profiles)

    # After 2 failures, candidates() should exclude inst-1 because its circuit breaker opened,
    # while inst-2 remains healthy
    candidates = await router.candidates(profiles)
    candidate_providers = [c[1].provider for c in candidates]
    assert "inst-1" not in candidate_providers
    assert "inst-2" in candidate_providers

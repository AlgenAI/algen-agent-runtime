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

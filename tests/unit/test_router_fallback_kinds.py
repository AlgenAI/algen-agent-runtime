"""Which provider errors fall back to the next model candidate, and which are raised."""

from __future__ import annotations

import json

import httpx
import pytest

from algen_agent_runtime import MeterCall
from algen_agent_runtime.exceptions.errors import ProviderError
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.models.providers.openai_compatible import OpenAICompatibleProvider
from algen_agent_runtime.types.contracts import (
    ErrorKind,
    Message,
    ModelCapabilities,
    ModelProfile,
    ModelRequest,
    ModelResponse,
    Role,
    TokenUsage,
)


class Meter:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self._tickets: dict[int, str] = {}

    async def reserve(
        self, *, max_input_tokens: int, max_output_tokens: int, call: MeterCall
    ) -> object:
        ticket = len(self._tickets)
        self._tickets[ticket] = call.provider or ""
        self.events.append(("reserve", call.provider or ""))
        return ticket

    async def settle(self, ticket: object, usage: TokenUsage) -> None:
        assert isinstance(ticket, int)
        self.events.append(("settle", self._tickets[ticket]))

    async def release(self, ticket: object) -> None:
        assert isinstance(ticket, int)
        self.events.append(("release", self._tickets[ticket]))


class Raising(MockModelProvider):
    def __init__(self, error: ProviderError) -> None:
        super().__init__()
        self.error = error

    async def generate(self, request: ModelRequest) -> ModelResponse:
        raise self.error


PROFILES = (
    ModelProfile(name="p", provider="primary", model="m", routing_preference="ordered"),
    ModelProfile(name="f", provider="fallback", model="m", routing_preference="ordered"),
)
REQUEST = ModelRequest(messages=(Message.text(Role.USER, "x"),))


def _router(primary: MockModelProvider) -> tuple[ModelRouter, Meter]:
    meter = Meter()
    router = ModelRouter(usage_meter=meter)
    router.register_provider(primary, registration_id="primary")
    router.register_provider(MockModelProvider(["from fallback"]), registration_id="fallback")
    return router, meter


@pytest.mark.parametrize(
    "error",
    [
        ProviderError("bad key", ErrorKind.AUTHENTICATION, False, status_code=401),
        ProviderError("forbidden", ErrorKind.AUTHORIZATION, False, status_code=403),
        ProviderError("slow down", ErrorKind.RATE_LIMIT, True, status_code=429),
        ProviderError("timed out", ErrorKind.TIMEOUT, True),
        ProviderError("down", ErrorKind.UNAVAILABLE, True, status_code=503),
        ProviderError("no such model", ErrorKind.INVALID_REQUEST, False, status_code=404),
        ProviderError("too large", ErrorKind.INVALID_REQUEST, False, status_code=413),
        ProviderError("garbled", ErrorKind.INVALID_RESPONSE, False),
    ],
    ids=lambda error: f"{error.error_kind}-{error.status_code}",
)
async def test_provider_specific_errors_fall_back(error: ProviderError) -> None:
    router, meter = _router(Raising(error))
    response = await router.generate(REQUEST, PROFILES)
    assert response.message.text_content == "from fallback"
    assert meter.events == [
        ("reserve", "primary"),
        ("release", "primary"),
        ("reserve", "fallback"),
        ("settle", "fallback"),
    ]


@pytest.mark.parametrize("status_code", [400, 422, None])
async def test_rejected_request_is_raised_without_fallback(status_code: int | None) -> None:
    error = ProviderError("bad schema", ErrorKind.INVALID_REQUEST, False, status_code=status_code)
    router, meter = _router(Raising(error))
    with pytest.raises(ProviderError) as captured:
        await router.generate(REQUEST, PROFILES)
    assert captured.value is error
    assert meter.events == [("reserve", "primary"), ("release", "primary")]
    # The request was at fault, not the provider.
    assert router._health["primary"].consecutive_failures == 0


async def test_http_status_reaches_the_router() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        status = int(json.loads(request.content)["model"])
        return httpx.Response(status, json={"error": {"message": "nope"}})

    provider = OpenAICompatibleProvider(
        "compat",
        "https://compat.example.test/v1",
        None,
        "400",
        ModelCapabilities(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    for status, kind in (
        (400, ErrorKind.INVALID_REQUEST),
        (404, ErrorKind.INVALID_REQUEST),
        (408, ErrorKind.TIMEOUT),
        (401, ErrorKind.AUTHENTICATION),
    ):
        with pytest.raises(ProviderError) as captured:
            await provider.generate(REQUEST.model_copy(update={"model": str(status)}))
        assert captured.value.status_code == status
        assert captured.value.error_kind == kind

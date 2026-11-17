"""Cancelling a model call stops the provider's HTTP request, for every built-in HTTP provider.

Each provider talks to a local fake API (``tests/fake_model_server.py``) that streams a couple of
chunks and then stalls, or holds a plain call open, and records when the client hangs up. A
cancelled, closed or timed-out call must close its response before the cancellation finishes
(not when the garbage collector next runs) and the server must see the connection go.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from typing import Any

import httpx
import pytest
from fake_model_server import FakeModelServer, TrackingTransport, no_tasks_left, tracked_client

from algen_agent_runtime.exceptions.errors import ProviderError
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.providers.adapters import (
    AnthropicProvider,
    AzureOpenAIProvider,
    DeepSeekProvider,
    GeminiProvider,
    GroqProvider,
    HuggingFaceInferenceProvider,
    MistralProvider,
    OllamaProvider,
    OpenAIProvider,
    XAIProvider,
)
from algen_agent_runtime.models.providers.openai_compatible import OpenAICompatibleProvider
from algen_agent_runtime.types.contracts import (
    ErrorKind,
    Message,
    ModelCapabilities,
    ModelProfile,
    ModelRequest,
    ModelStreamEvent,
    Role,
)

HANG_UP_SECONDS = 2.0


class FakeSecrets:
    """Resolves every reference to a placeholder: no real key is ever used."""

    async def get(self, reference: str) -> str:
        return "fake-key-for-tests"


Factory = Callable[[str, httpx.AsyncClient], Any]


def _based(provider: OpenAICompatibleProvider, url: str) -> OpenAICompatibleProvider:
    # OpenAI and DeepSeek have a fixed base URL: point it at the fake server.
    provider._base_url = url
    return provider


PROVIDERS: dict[str, tuple[Factory, bool]] = {
    "openai_compatible": (
        lambda url, client: OpenAICompatibleProvider(
            "compatible", url, None, "fake-model", ModelCapabilities(streaming=True), client=client
        ),
        False,
    ),
    "openai": (
        lambda url, client: _based(
            OpenAIProvider(
                "env://FAKE_KEY", "gpt-test", secret_provider=FakeSecrets(), client=client
            ),
            url,
        ),
        False,
    ),
    "azure_openai": (
        lambda url, client: AzureOpenAIProvider(
            url, "env://FAKE_KEY", "deployment", "2024-10-21", FakeSecrets(), client
        ),
        False,
    ),
    "deepseek": (
        lambda url, client: _based(
            DeepSeekProvider("env://FAKE_KEY", secret_provider=FakeSecrets(), client=client), url
        ),
        False,
    ),
    "mistral": (
        lambda url, client: MistralProvider(
            "env://FAKE_KEY", base_url=url, secret_provider=FakeSecrets(), client=client
        ),
        False,
    ),
    "gemini": (
        lambda url, client: GeminiProvider(
            "env://FAKE_KEY", base_url=url, secret_provider=FakeSecrets(), client=client
        ),
        False,
    ),
    "xai": (
        lambda url, client: XAIProvider(
            "env://FAKE_KEY", base_url=url, secret_provider=FakeSecrets(), client=client
        ),
        False,
    ),
    "groq": (
        lambda url, client: GroqProvider(
            "env://FAKE_KEY", base_url=url, secret_provider=FakeSecrets(), client=client
        ),
        False,
    ),
    "huggingface": (
        lambda url, client: HuggingFaceInferenceProvider(
            "env://FAKE_KEY",
            "fake-model",
            base_url=url,
            secret_provider=FakeSecrets(),
            client=client,
        ),
        False,
    ),
    "ollama": (
        lambda url, client: OllamaProvider("fake-model", base_url=url, client=client),
        False,
    ),
    "anthropic": (
        lambda url, client: AnthropicProvider(
            "env://FAKE_KEY", "fake-model", url, secret_provider=FakeSecrets(), client=client
        ),
        True,
    ),
}


def _request(*, stream: bool, timeout: float | None = None) -> ModelRequest:
    return ModelRequest(
        messages=(Message.text(Role.USER, "hello"),),
        model="fake-model",
        stream=stream,
        timeout_seconds=timeout,
    )


async def _consume(events: AsyncIterator[ModelStreamEvent], seen: asyncio.Event) -> None:
    async for event in events:
        if event.delta:
            seen.set()


async def _assert_hung_up(server: FakeModelServer, transport: TrackingTransport) -> None:
    # The client closed its response before the cancellation finished...
    assert transport.open_responses == 0
    # ...and the server saw the connection go.
    await asyncio.wait_for(server.last.disconnected.wait(), HANG_UP_SECONDS)
    assert not server.last.finished


@pytest.fixture(params=sorted(PROVIDERS))
def provider_kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


async def test_cancelling_a_stream_closes_the_provider_request(provider_kind: str) -> None:
    factory, anthropic = PROVIDERS[provider_kind]
    client, transport = tracked_client()
    async with FakeModelServer(anthropic=anthropic) as server:
        provider = factory(server.url, client)
        seen = asyncio.Event()
        task = asyncio.create_task(_consume(provider.stream(_request(stream=True)), seen))
        await server.wait_for_request()
        await asyncio.wait_for(seen.wait(), HANG_UP_SECONDS)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _assert_hung_up(server, transport)
    await client.aclose()
    await no_tasks_left()


async def test_cancelling_a_call_closes_the_provider_request(provider_kind: str) -> None:
    factory, anthropic = PROVIDERS[provider_kind]
    client, transport = tracked_client()
    async with FakeModelServer(anthropic=anthropic, delay=None) as server:
        provider = factory(server.url, client)
        task = asyncio.create_task(provider.generate(_request(stream=False)))
        await server.wait_for_request()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _assert_hung_up(server, transport)
    await client.aclose()
    await no_tasks_left()


async def test_a_stream_closed_by_its_consumer_closes_the_provider_request(
    provider_kind: str,
) -> None:
    # The consumer stops between two events (a cancellation while it handles one, or a
    # response-size limit): closing the stream closes the response there and then.
    factory, anthropic = PROVIDERS[provider_kind]
    client, transport = tracked_client()
    async with FakeModelServer(anthropic=anthropic) as server:
        provider = factory(server.url, client)
        events = provider.stream(_request(stream=True))
        first = await anext(events)
        assert first.delta
        await events.aclose()
        await _assert_hung_up(server, transport)
    await client.aclose()
    await no_tasks_left()


async def test_a_router_stream_closed_early_closes_the_provider_request(
    provider_kind: str,
) -> None:
    # The Runtime consumes the router's stream, which consumes the provider's.
    factory, anthropic = PROVIDERS[provider_kind]
    client, transport = tracked_client()
    async with FakeModelServer(anthropic=anthropic) as server:
        provider = factory(server.url, client)
        router = ModelRouter()
        router.register_provider(provider)
        profile = ModelProfile(name="p", provider=provider.provider_id, model="fake-model")
        async with aclosing(router.stream(_request(stream=True), [profile])) as events:
            async for event in events:
                assert event.delta
                break
        await _assert_hung_up(server, transport)
    await client.aclose()
    await no_tasks_left()


async def test_a_stalled_stream_times_out_and_closes_the_provider_request(
    provider_kind: str,
) -> None:
    factory, anthropic = PROVIDERS[provider_kind]
    client, transport = tracked_client()
    async with FakeModelServer(anthropic=anthropic) as server:
        provider = factory(server.url, client)
        with pytest.raises(ProviderError) as raised:
            await _consume(provider.stream(_request(stream=True, timeout=0.3)), asyncio.Event())
        assert raised.value.error_kind == ErrorKind.TIMEOUT
        assert raised.value.retryable
        await _assert_hung_up(server, transport)
    await client.aclose()
    await no_tasks_left()


async def test_a_stalled_call_times_out_and_closes_the_provider_request(
    provider_kind: str,
) -> None:
    factory, anthropic = PROVIDERS[provider_kind]
    client, transport = tracked_client()
    async with FakeModelServer(anthropic=anthropic, delay=None) as server:
        provider = factory(server.url, client)
        with pytest.raises(ProviderError) as raised:
            await provider.generate(_request(stream=False, timeout=0.3))
        assert raised.value.error_kind == ErrorKind.TIMEOUT
        await _assert_hung_up(server, transport)
    await client.aclose()
    await no_tasks_left()


async def test_the_fake_server_completes_a_call_that_is_not_cancelled(provider_kind: str) -> None:
    # The same path, left alone, finishes: the tests above fail for the cancellation only.
    factory, anthropic = PROVIDERS[provider_kind]
    client, transport = tracked_client()
    async with FakeModelServer(anthropic=anthropic, finish=True, delay=0) as server:
        provider = factory(server.url, client)
        events = [event async for event in provider.stream(_request(stream=True))]
        assert events[-1].response is not None
        assert events[-1].response.message.text_content.startswith("token0")
        response = await provider.generate(_request(stream=False))
        assert response.message.text_content == "done"
        assert all(exchange.finished for exchange in server.exchanges)
        assert transport.open_responses == 0
    await client.aclose()


async def test_a_refused_stream_connection_is_a_retryable_provider_error(
    provider_kind: str,
) -> None:
    # Anthropic's stream let an httpx transport error escape, which the router cannot fall
    # back from; every provider now reports it as a retryable ``unavailable`` ProviderError.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    factory, _ = PROVIDERS[provider_kind]
    async with httpx.AsyncClient() as client:
        provider = factory(f"http://127.0.0.1:{port}", client)
        with pytest.raises(ProviderError) as raised:
            await _consume(provider.stream(_request(stream=True)), asyncio.Event())
    assert raised.value.error_kind == ErrorKind.UNAVAILABLE
    assert raised.value.retryable

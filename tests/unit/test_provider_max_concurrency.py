"""``providers.<name>.max_concurrency`` caps the calls in flight on a provider (0.2.0b5).

The setting was documented and validated but never read, so a provider took any number of
concurrent calls. It is enforced when set explicitly; the field's default sets no cap, as before.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.types.contracts import (
    Message,
    ModelProfile,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    Role,
)


class _Counting(MockModelProvider):
    """Records how many calls run at once."""

    def __init__(self) -> None:
        super().__init__(delay_seconds=0.02)
        self.active = 0
        self.peak = 0

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            return await super().generate(request)
        finally:
            self.active -= 1

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        # The whole stream counts as one call in flight.
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            response = await MockModelProvider.generate(self, request)
            for word in response.message.text_content.split():
                yield ModelStreamEvent(type="delta", delta=word + " ")
                await asyncio.sleep(0.01)
            yield ModelStreamEvent(type="completed", response=response)
        finally:
            self.active -= 1


def _router(provider: MockModelProvider, max_concurrency: int | None) -> ModelRouter:
    router = ModelRouter()
    router.register_provider(provider, registration_id="p", max_concurrency=max_concurrency)
    return router


PROFILE = (ModelProfile(name="m", provider="p", model="deterministic"),)
REQUEST = ModelRequest(messages=(Message.text(Role.USER, "hello"),))


async def test_plain_calls_wait_for_a_slot() -> None:
    provider = _Counting()
    router = _router(provider, 2)
    await asyncio.gather(*(router.generate(REQUEST, PROFILE) for _ in range(6)))
    assert provider.peak == 2


async def test_an_open_stream_holds_its_slot_until_it_closes() -> None:
    provider = _Counting()
    router = _router(provider, 1)

    async def consume() -> None:
        async for _ in router.stream(REQUEST, PROFILE):
            await asyncio.sleep(0.01)

    await asyncio.gather(consume(), consume(), router.generate(REQUEST, PROFILE))
    assert provider.peak == 1


async def test_a_stream_closed_early_releases_its_slot() -> None:
    provider = _Counting()
    router = _router(provider, 1)
    stream = router.stream(REQUEST, PROFILE)
    await anext(stream)
    await stream.aclose()  # type: ignore[attr-defined]
    await asyncio.wait_for(router.generate(REQUEST, PROFILE), timeout=1)


async def test_no_cap_without_max_concurrency() -> None:
    provider = _Counting()
    router = _router(provider, None)
    await asyncio.gather(*(router.generate(REQUEST, PROFILE) for _ in range(6)))
    assert provider.peak == 6


def test_the_container_enforces_only_an_explicit_setting() -> None:
    def slots(**provider: object) -> dict[str, asyncio.Semaphore]:
        settings = AppSettings.model_validate(
            {"providers": {"p": {"type": "mock", "default_model": "m", **provider}}}
        )
        container = build_container(settings)
        try:
            return dict(container.runtime.router._slots)
        finally:
            container.close()

    assert slots() == {}
    assert slots(max_concurrency=3)["p"]._value == 3

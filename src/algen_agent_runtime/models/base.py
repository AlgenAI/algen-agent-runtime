from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from algen_agent_runtime.cache import CacheContext, CacheService
from algen_agent_runtime.exceptions.errors import CapabilityError, ProviderError
from algen_agent_runtime.types.contracts import (
    ErrorKind,
    ModelCapabilities,
    ModelProfile,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
)
from algen_agent_runtime.types.interfaces import ModelProvider


@dataclass
class ProviderHealth:
    consecutive_failures: int = 0
    opened_at: float | None = None
    latency_ms: float = 0


class SlidingWindowRateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60) -> None:
        self._limit = limit
        self._window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._timestamps and self._timestamps[0] <= now - self._window:
                    self._timestamps.popleft()
                if len(self._timestamps) < self._limit:
                    self._timestamps.append(now)
                    return
                delay = self._timestamps[0] + self._window - now
            await asyncio.sleep(max(0.001, delay))


class ModelRouter:
    def __init__(
        self,
        circuit_failure_threshold: int = 5,
        circuit_reset_seconds: float = 30,
        cache: CacheService | None = None,
    ) -> None:
        self._providers: dict[str, ModelProvider] = {}
        self._profiles: dict[str, ModelProfile] = {}
        self._health: dict[str, ProviderHealth] = {}
        self._limits: dict[str, SlidingWindowRateLimiter] = {}
        self._costs: dict[str, tuple[float, float]] = {}
        self._local: dict[str, bool] = {}
        self._threshold = circuit_failure_threshold
        self._reset_seconds = circuit_reset_seconds
        self._cache = cache

    async def _capabilities(self, provider: ModelProvider, model: str) -> ModelCapabilities:
        if self._cache is None:
            return await provider.capabilities(model)

        async def discover() -> dict[str, object]:
            value = await provider.capabilities(model)
            return value.model_dump(mode="json")

        value, _ = await self._cache.get_or_set_json(
            "model_capabilities",
            "model.capabilities",
            {"provider": provider.provider_id, "model": model},
            CacheContext(),
            discover,
            tags=(f"provider:{provider.provider_id}",),
        )
        return ModelCapabilities.model_validate(value)

    def register_provider(
        self,
        provider: ModelProvider,
        requests_per_minute: int = 600,
        cost_per_1k_input: float = 0,
        cost_per_1k_output: float = 0,
        is_local: bool = False,
    ) -> None:
        self._providers[provider.provider_id] = provider
        self._health[provider.provider_id] = ProviderHealth()
        self._limits[provider.provider_id] = SlidingWindowRateLimiter(requests_per_minute)
        self._costs[provider.provider_id] = (cost_per_1k_input, cost_per_1k_output)
        self._local[provider.provider_id] = is_local

    def register_profile(self, profile: ModelProfile) -> None:
        self._profiles[profile.name] = profile

    def provider(self, provider_id: str) -> ModelProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise CapabilityError(f"provider {provider_id!r} is not registered") from exc

    async def candidates(
        self, profiles: Sequence[ModelProfile], allowlist: frozenset[str] = frozenset()
    ) -> tuple[tuple[ModelProvider, ModelProfile], ...]:
        ranked: list[tuple[float, ModelProvider, ModelProfile]] = []
        now = time.monotonic()
        for profile in profiles:
            if profile.provider is None or profile.model is None:
                continue
            identity = f"{profile.provider}/{profile.model}"
            if allowlist and identity not in allowlist:
                continue
            provider = self._providers.get(profile.provider)
            if provider is None:
                continue
            health = self._health[profile.provider]
            if health.opened_at and now - health.opened_at < self._reset_seconds:
                continue
            if profile.max_latency_ms is not None and health.latency_ms > profile.max_latency_ms:
                continue
            capabilities = await self._capabilities(provider, profile.model)
            if not all(
                bool(getattr(capabilities, item, False)) for item in profile.required_capabilities
            ):
                continue
            input_cost, output_cost = self._costs[provider.provider_id]
            average_cost = (input_cost + output_cost) / 2
            if (
                profile.max_cost_per_1k_tokens is not None
                and average_cost > profile.max_cost_per_1k_tokens
            ):
                continue
            locality = self._local[provider.provider_id]
            preference_score = 0
            if profile.routing_preference == "local_first":
                preference_score = 10_000 if locality else 0
            elif profile.routing_preference == "cloud_first":
                preference_score = 10_000 if not locality else 0
            score = (
                profile.quality_tier * 1000 + preference_score - health.latency_ms - average_cost
            )
            ranked.append((score, provider, profile))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return tuple((provider, profile) for _, provider, profile in ranked)

    async def generate(
        self,
        request: ModelRequest,
        profiles: Sequence[ModelProfile],
        allowlist: frozenset[str] = frozenset(),
    ) -> ModelResponse:
        candidates = await self.candidates(profiles, allowlist)
        if not candidates:
            raise CapabilityError("no healthy allowed model satisfies required capabilities")
        errors: list[str] = []
        failures: list[ProviderError] = []
        for provider, profile in candidates:
            await self._limits[provider.provider_id].acquire()
            started = time.monotonic()
            try:
                response = await provider.generate(
                    request.model_copy(
                        update={"model": profile.model, "extensions": profile.extensions}
                    )
                )
                health = self._health[provider.provider_id]
                health.consecutive_failures = 0
                health.opened_at = None
                health.latency_ms = (time.monotonic() - started) * 1000
                input_cost, output_cost = self._costs[provider.provider_id]
                estimated_cost = (
                    response.usage.input_tokens * input_cost
                    + response.usage.output_tokens * output_cost
                ) / 1000
                return response.model_copy(
                    update={
                        "usage": response.usage.model_copy(
                            update={"estimated_cost_usd": estimated_cost}
                        )
                    }
                )
            except ProviderError as exc:
                errors.append(f"{provider.provider_id}: {exc}")
                failures.append(exc)
                health = self._health[provider.provider_id]
                health.consecutive_failures += 1
                if health.consecutive_failures >= self._threshold:
                    health.opened_at = time.monotonic()
                if not exc.retryable:
                    continue
        kinds = {failure.error_kind for failure in failures}
        kind = failures[-1].error_kind if len(kinds) == 1 else ErrorKind.UNKNOWN
        retryable = bool(failures) and all(failure.retryable for failure in failures)
        raise ProviderError("all model candidates failed: " + "; ".join(errors), kind, retryable)

    def providers(self) -> tuple[ModelProvider, ...]:
        return tuple(self._providers[key] for key in sorted(self._providers))

    async def stream(
        self,
        request: ModelRequest,
        profiles: Sequence[ModelProfile],
        allowlist: frozenset[str] = frozenset(),
    ) -> AsyncIterator[ModelStreamEvent]:
        candidates = await self.candidates(profiles, allowlist)
        if not candidates:
            raise CapabilityError("no healthy allowed streaming model is available")
        provider, profile = candidates[0]
        capabilities = await self._capabilities(provider, profile.model or "")
        if not capabilities.streaming:
            raise CapabilityError(
                f"{provider.provider_id}/{profile.model} does not support streaming"
            )
        await self._limits[provider.provider_id].acquire()
        async for event in provider.stream(
            request.model_copy(
                update={"model": profile.model, "stream": True, "extensions": profile.extensions}
            )
        ):
            if event.response:
                input_cost, output_cost = self._costs[provider.provider_id]
                usage = event.response.usage
                estimated_cost = (
                    usage.input_tokens * input_cost + usage.output_tokens * output_cost
                ) / 1000
                event = event.model_copy(
                    update={
                        "response": event.response.model_copy(
                            update={
                                "usage": usage.model_copy(
                                    update={"estimated_cost_usd": estimated_cost}
                                )
                            }
                        )
                    }
                )
            yield event

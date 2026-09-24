from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

import structlog

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

logger = structlog.get_logger("algen_agent_runtime.models")


def _narrow_capabilities(
    adapter_caps: ModelCapabilities,
    override: ModelCapabilities | None,
    registration_id: str,
    model: str,
) -> ModelCapabilities:
    """Apply capability narrowing: C_effective = C_adapter ∩ C_configured.

    Configuration may narrow True→False; it must never widen False→True.
    Attempts to widen an unsupported capability are logged and kept False.
    """
    if override is None:
        return adapter_caps
    effective: dict[str, bool] = {}
    for field_name in ModelCapabilities.model_fields:
        adapter_val = bool(getattr(adapter_caps, field_name, False))
        if field_name not in override.model_fields_set:
            effective[field_name] = adapter_val
            continue
        configured_val = bool(getattr(override, field_name, False))
        if configured_val and not adapter_val:
            logger.warning(
                "cannot_widen_unsupported_capability",
                provider=registration_id,
                model=model,
                capability=field_name,
                message=(
                    f"Configured capability {field_name}=True for provider {registration_id!r} "
                    f"ignored because the adapter reports it as unsupported"
                ),
            )
        effective[field_name] = adapter_val and configured_val
    return ModelCapabilities.model_validate(effective)


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
        # WP-02: per-registration capability overrides applied before cache / adapter call.
        self._capability_overrides: dict[str, ModelCapabilities] = {}
        # WP-01: maps registration_id → provider so the adapter's own provider_id is preserved
        # for telemetry / protocol behavior while the router key is the deployment-chosen name.
        self._registration_ids: dict[str, str] = {}  # registration_id → provider.provider_id
        self._threshold = circuit_failure_threshold
        self._reset_seconds = circuit_reset_seconds
        self._cache = cache

    async def _capabilities(
        self,
        registration_id: str,
        provider: ModelProvider,
        model: str,
    ) -> ModelCapabilities:
        override = self._capability_overrides.get(registration_id)
        override_payload = (
            override.model_dump(mode="json", exclude_unset=True) if override is not None else None
        )

        if self._cache is None:
            raw_caps = await provider.capabilities(model)
            return _narrow_capabilities(raw_caps, override, registration_id, model)

        # WP-01 & WP-02: key capability cache by registration_id and capability override policy
        async def discover() -> dict[str, object]:
            value = await provider.capabilities(model)
            return value.model_dump(mode="json")

        value, _ = await self._cache.get_or_set_json(
            "model_capabilities",
            "model.capabilities",
            {
                "provider": registration_id,
                "model": model,
                "override": override_payload,
            },
            CacheContext(),
            discover,
            tags=(f"provider:{registration_id}",),
        )
        raw_caps = ModelCapabilities.model_validate(value)
        return _narrow_capabilities(raw_caps, override, registration_id, model)

    def register_provider(
        self,
        provider: ModelProvider,
        requests_per_minute: int = 600,
        cost_per_1k_input: float = 0,
        cost_per_1k_output: float = 0,
        is_local: bool = False,
        *,
        registration_id: str | None = None,
        capability_override: ModelCapabilities | None = None,
    ) -> None:
        """Register a provider instance.

        ``registration_id`` sets the key used for routing, health tracking, rate
        limiting, and cost accounting. It defaults to ``provider.provider_id`` for
        backward compatibility.  Pass an explicit value (typically the YAML
        configuration key) when registering multiple instances of the same
        provider type so they remain independently addressable.

        Registering the same ``registration_id`` twice raises ``ValueError``.

        ``capability_override`` pins the effective capabilities returned for every
        model served by this registration, applying the configured narrowing policy
        (WP-02). When *None*, the adapter's ``capabilities()`` method is called
        normally.
        """
        rid = registration_id if registration_id is not None else provider.provider_id
        if rid in self._providers:
            raise ValueError(
                f"provider {rid!r} is already registered; use a unique registration_id "
                "for multiple instances of the same adapter type"
            )
        self._providers[rid] = provider
        self._health[rid] = ProviderHealth()
        self._limits[rid] = SlidingWindowRateLimiter(requests_per_minute)
        self._costs[rid] = (cost_per_1k_input, cost_per_1k_output)
        self._local[rid] = is_local
        self._registration_ids[rid] = provider.provider_id
        if capability_override is not None:
            self._capability_overrides[rid] = capability_override

    def register_profile(self, profile: ModelProfile) -> None:
        self._profiles[profile.name] = profile

    def provider(self, provider_id: str) -> ModelProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise CapabilityError(f"provider {provider_id!r} is not registered") from exc

    def _registration_id_for(self, provider: ModelProvider) -> str:
        """Return the registration key for a provider that has already been registered."""
        # _providers is keyed by registration_id, so a reverse lookup is needed.
        # Build it lazily from the existing dict: this is O(n) but n is tiny.
        for rid, p in self._providers.items():
            if p is provider:
                return rid
        # Fallback (should never occur for a registered provider).
        return provider.provider_id

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
            # profile.provider is the registration_id (YAML key).
            rid = profile.provider
            provider = self._providers.get(rid)
            if provider is None:
                continue
            health = self._health[rid]
            if health.opened_at and now - health.opened_at < self._reset_seconds:
                continue
            if profile.max_latency_ms is not None and health.latency_ms > profile.max_latency_ms:
                continue
            capabilities = await self._capabilities(rid, provider, profile.model)
            if not all(
                bool(getattr(capabilities, item, False)) for item in profile.required_capabilities
            ):
                continue
            input_cost, output_cost = self._costs[rid]
            average_cost = (input_cost + output_cost) / 2
            if (
                profile.max_cost_per_1k_tokens is not None
                and average_cost > profile.max_cost_per_1k_tokens
            ):
                continue
            locality = self._local[rid]
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
            # profile.provider is the registration_id.
            rid = profile.provider or self._registration_id_for(provider)
            await self._limits[rid].acquire()
            started = time.monotonic()
            try:
                response = await provider.generate(
                    request.model_copy(
                        update={"model": profile.model, "extensions": profile.extensions}
                    )
                )
                health = self._health[rid]
                health.consecutive_failures = 0
                health.opened_at = None
                health.latency_ms = (time.monotonic() - started) * 1000
                input_cost, output_cost = self._costs[rid]
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
                errors.append(f"{rid}: {exc}")
                failures.append(exc)
                health = self._health[rid]
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
        rid = profile.provider or self._registration_id_for(provider)
        capabilities = await self._capabilities(rid, provider, profile.model or "")
        if not capabilities.streaming:
            raise CapabilityError(f"{rid}/{profile.model} does not support streaming")
        await self._limits[rid].acquire()
        async for event in provider.stream(
            request.model_copy(
                update={"model": profile.model, "stream": True, "extensions": profile.extensions}
            )
        ):
            if event.response:
                input_cost, output_cost = self._costs[rid]
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

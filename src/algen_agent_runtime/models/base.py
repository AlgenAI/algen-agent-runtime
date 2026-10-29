from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import cast

import structlog

from algen_agent_runtime.cache import CacheContext, CacheService
from algen_agent_runtime.context.budget import estimate_request_tokens, prepare_model_request
from algen_agent_runtime.exceptions.errors import (
    BudgetExceededError,
    CapabilityError,
    MeterRefusedError,
    ProviderError,
)
from algen_agent_runtime.models.metering import (
    DEFAULT_RESERVED_OUTPUT_TOKENS,
    MeterCall,
    UsageMeter,
    reserve_accepts_call,
)
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


_REQUEST_REJECTED_STATUSES = frozenset({400, 422})


def _rejects_request(exc: ProviderError) -> bool:
    """Whether ``exc`` rejects the request itself, so another provider would fail the same way."""
    return exc.error_kind == ErrorKind.INVALID_REQUEST and (
        exc.status_code is None or exc.status_code in _REQUEST_REJECTED_STATUSES
    )


class ModelRouter:
    def __init__(
        self,
        circuit_failure_threshold: int = 5,
        circuit_reset_seconds: float = 30,
        cache: CacheService | None = None,
        usage_meter: UsageMeter | None = None,
        reserve_input_margin: float = 1.0,
    ) -> None:
        """``reserve_input_margin`` scales the estimated input tokens the ``usage_meter``
        reserves (``estimate_request_tokens`` assumes four characters a token, and code, JSON
        and non-English text often run under that). It applies only to metered reservations,
        never to context packing or the run's token budget; it must be at least 1.0.
        """
        if not math.isfinite(reserve_input_margin) or reserve_input_margin < 1.0:
            raise ValueError(
                f"reserve_input_margin must be a finite number >= 1.0, not {reserve_input_margin}"
            )
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
        # Reserves before and settles after every provider dispatch; see models/metering.py.
        self._usage_meter = usage_meter
        self._reserve_input_margin = reserve_input_margin
        # Inspected once here, never per call: a 0.1.0a14 meter is called without ``call``.
        self._meter_takes_call = usage_meter is None or reserve_accepts_call(usage_meter)

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
        # "ordered" describes the whole list, so the first (default) profile decides it. Settings
        # reject mixed lists; an "ordered" fallback behind a scored default is scored as balanced.
        ordered = bool(profiles) and profiles[0].routing_preference == "ordered"
        for position, profile in enumerate(profiles):
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
            if ordered:
                # Declaration order wins (primary, then fallbacks); health, capability, latency
                # and cost filters above still remove candidates.
                score = -float(position)
            else:
                score = (
                    profile.quality_tier * 1000
                    + preference_score
                    - health.latency_ms
                    - average_cost
                )
            ranked.append((score, provider, profile))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return tuple((provider, profile) for _, provider, profile in ranked)

    async def generate(
        self,
        request: ModelRequest,
        profiles: Sequence[ModelProfile],
        allowlist: frozenset[str] = frozenset(),
        *,
        meter_call: MeterCall | None = None,
    ) -> ModelResponse:
        """Dispatch to the best candidate, falling back to the next on a provider error.

        Authentication and authorization failures, rate limits, timeouts, unavailability and
        errors only one provider raises (such as a 404 for a model it does not serve) fall back.
        A request rejected as invalid (HTTP 400/422 ``invalid_request``) is raised as is, since
        every candidate would reject it.

        ``meter_call`` is the run context the usage meter receives; the router fills in
        ``provider``, ``model`` and ``streaming`` for each candidate it tries.
        """
        candidates = await self.candidates(profiles, allowlist)
        if not candidates:
            raise CapabilityError("no healthy allowed model satisfies required capabilities")
        errors: list[str] = []
        failures: list[ProviderError] = []
        for provider, profile in candidates:
            # profile.provider is the registration_id.
            rid = profile.provider or self._registration_id_for(provider)
            try:
                candidate_request = prepare_model_request(request, profile)
            except BudgetExceededError:
                errors.append(f"{rid}/{profile.model}: configured context budget exceeded")
                continue
            await self._limits[rid].acquire()
            # A refusal is not a ProviderError: it skips the fallbacks below and leaves health
            # untouched.
            ticket = await self._reserve(
                candidate_request, meter_call, rid, profile, streaming=False
            )
            started = time.monotonic()
            try:
                response = await self._dispatch(provider, candidate_request, ticket)
                health = self._health[rid]
                health.consecutive_failures = 0
                health.opened_at = None
                health.latency_ms = (time.monotonic() - started) * 1000
                input_cost, output_cost = self._costs[rid]
                estimated_cost = (
                    response.usage.input_tokens * input_cost
                    + response.usage.output_tokens * output_cost
                ) / 1000
                priced = response.model_copy(
                    update={
                        "usage": response.usage.model_copy(
                            update={"estimated_cost_usd": estimated_cost}
                        )
                    }
                )
                if self._usage_meter is not None:
                    await self._usage_meter.settle(ticket, priced.usage)
                return priced
            except ProviderError as exc:
                if _rejects_request(exc):
                    # Every provider would reject this request the same way, so falling back
                    # would only hide the error behind another model's answer. The request is at
                    # fault, not the provider, so its health is untouched. The reservation was
                    # released by _dispatch.
                    raise
                errors.append(f"{rid}: {exc}")
                failures.append(exc)
                health = self._health[rid]
                health.consecutive_failures += 1
                if health.consecutive_failures >= self._threshold:
                    health.opened_at = time.monotonic()
        if not failures:
            raise BudgetExceededError("no model candidate fits the configured context budget")
        kinds = {failure.error_kind for failure in failures}
        kind = failures[-1].error_kind if len(kinds) == 1 else ErrorKind.UNKNOWN
        retryable = bool(failures) and all(failure.retryable for failure in failures)
        raise ProviderError("all model candidates failed: " + "; ".join(errors), kind, retryable)

    def providers(self) -> tuple[ModelProvider, ...]:
        return tuple(self._providers[key] for key in sorted(self._providers))

    def registration_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    async def supports_streaming(
        self, profiles: Sequence[ModelProfile], allowlist: frozenset[str] = frozenset()
    ) -> bool:
        """Whether every candidate ``stream`` could pick for these profiles can stream.

        ``stream`` never falls back to ``generate``, so a caller that streams only when it can
        (such as a conversation turn) asks this first and otherwise generates.
        """
        candidates = await self.candidates(profiles, allowlist)
        if not candidates:
            return False
        for provider, profile in candidates:
            rid = profile.provider or self._registration_id_for(provider)
            capabilities = await self._capabilities(rid, provider, profile.model or "")
            if not capabilities.streaming:
                return False
        return True

    async def stream(
        self,
        request: ModelRequest,
        profiles: Sequence[ModelProfile],
        allowlist: frozenset[str] = frozenset(),
        *,
        meter_call: MeterCall | None = None,
    ) -> AsyncGenerator[ModelStreamEvent, None]:
        """Stream from the first candidate that fits; see ``generate`` for ``meter_call``."""
        candidates = await self.candidates(profiles, allowlist)
        if not candidates:
            raise CapabilityError("no healthy allowed streaming model is available")
        # Only preflight selection can fall back. Never replay a stream once the
        # provider has started delivering events to the caller.
        for candidate_provider, profile in candidates:
            try:
                candidate_request = prepare_model_request(request, profile)
            except BudgetExceededError:
                continue
            provider = candidate_provider
            break
        else:
            raise BudgetExceededError(
                "no streaming model candidate fits the configured context budget"
            )
        rid = profile.provider or self._registration_id_for(provider)
        capabilities = await self._capabilities(rid, provider, profile.model or "")
        if not capabilities.streaming:
            raise CapabilityError(f"{rid}/{profile.model} does not support streaming")
        await self._limits[rid].acquire()
        ticket = await self._reserve(candidate_request, meter_call, rid, profile, streaming=True)
        settled = False
        try:
            async for event in provider.stream(
                candidate_request.model_copy(update={"stream": True})
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
                    if self._usage_meter is not None and not settled:
                        assert event.response is not None
                        await self._usage_meter.settle(ticket, event.response.usage)
                        settled = True
                yield event
        finally:
            # An error, cancellation or a consumer that stops early ends the stream unsettled.
            if self._usage_meter is not None and not settled:
                await self._usage_meter.release(ticket)

    async def _reserve(
        self,
        request: ModelRequest,
        meter_call: MeterCall | None,
        registration_id: str,
        profile: ModelProfile,
        *,
        streaming: bool,
    ) -> object:
        """Reserve a call's most cost with the meter, or nothing without one."""
        if self._usage_meter is None:
            return None
        model = request.model or profile.model or ""
        max_input_tokens = math.ceil(estimate_request_tokens(request) * self._reserve_input_margin)
        max_output_tokens = request.max_output_tokens or DEFAULT_RESERVED_OUTPUT_TOKENS
        try:
            if not self._meter_takes_call:
                # The 0.1.0a14 form, warned about when this router was built; removed in 0.2.
                legacy = cast(Callable[..., Awaitable[object]], self._usage_meter.reserve)
                return await legacy(
                    max_input_tokens=max_input_tokens, max_output_tokens=max_output_tokens
                )
            call = replace(
                meter_call if meter_call is not None else MeterCall(purpose="other"),
                provider=registration_id,
                model=model,
                streaming=streaming,
            )
            return await self._usage_meter.reserve(
                max_input_tokens=max_input_tokens,
                max_output_tokens=max_output_tokens,
                call=call,
            )
        except MeterRefusedError as exc:
            # Name the refused candidate for the run's budget.refused event and audit.
            exc.provider = registration_id
            exc.model = model
            raise

    async def _dispatch(
        self, provider: ModelProvider, request: ModelRequest, ticket: object
    ) -> ModelResponse:
        if self._usage_meter is None:
            return await provider.generate(request)
        try:
            return await provider.generate(request)
        except BaseException:
            await self._usage_meter.release(ticket)
            raise

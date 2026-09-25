from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

ROUTE_CLASS_READ = "read"
ROUTE_CLASS_WRITE = "write"
ROUTE_CLASS_RUN_CREATE = "run_create"


@dataclass(frozen=True)
class LimiterDecision:
    """Outcome of an admission rate-limiting or concurrency evaluation."""

    allowed: bool
    retry_after_seconds: float = 0.0
    remaining: int = 0
    limit: int = 0
    reason: str | None = None


class ApiRateLimiter(Protocol):
    """Protocol for API admission rate and concurrency limiting."""

    async def acquire(
        self,
        tenant_id: str,
        route_class: str,
        principal_id: str | None = None,
    ) -> LimiterDecision: ...

    async def release(
        self,
        tenant_id: str,
        route_class: str,
        principal_id: str | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class RouteLimitPolicy:
    """Rate and concurrency thresholds for a route class."""

    rate_per_minute: int
    burst: int
    max_concurrent: int | None = None


@dataclass
class _BucketState:
    tokens: float
    last_refill: float
    active_concurrency: int = 0


class InMemoryApiRateLimiter:
    """In-memory single-node admission rate limiter using token bucket and run-creation request concurrency tracking.

    Supports per-tenant quotas, route-class thresholds, in-flight request concurrency bounding,
    and injectable clock for deterministic testing.
    """

    def __init__(
        self,
        default_rate_per_minute: int = 120,
        default_burst: int = 30,
        max_concurrent_run_requests_per_tenant: int = 10,
        route_policies: dict[str, RouteLimitPolicy] | None = None,
        clock: Callable[[], float] = time.monotonic,
        fail_closed: bool = True,
    ) -> None:
        self.default_rate_per_minute = max(1, default_rate_per_minute)
        self.default_burst = max(1, default_burst)
        self.max_concurrent_run_requests_per_tenant = max(1, max_concurrent_run_requests_per_tenant)
        self.clock = clock
        self.fail_closed = fail_closed
        self._lock = asyncio.Lock()

        # Build policies mapping
        self._route_policies: dict[str, RouteLimitPolicy] = {
            ROUTE_CLASS_READ: RouteLimitPolicy(
                rate_per_minute=self.default_rate_per_minute,
                burst=self.default_burst,
                max_concurrent=None,
            ),
            ROUTE_CLASS_WRITE: RouteLimitPolicy(
                rate_per_minute=self.default_rate_per_minute,
                burst=self.default_burst,
                max_concurrent=None,
            ),
            ROUTE_CLASS_RUN_CREATE: RouteLimitPolicy(
                rate_per_minute=self.default_rate_per_minute,
                burst=self.default_burst,
                max_concurrent=self.max_concurrent_run_requests_per_tenant,
            ),
        }
        if route_policies:
            self._route_policies.update(route_policies)

        # state: (tenant_id, route_class) -> _BucketState
        self._states: dict[tuple[str, str], _BucketState] = {}

    def get_policy(self, route_class: str) -> RouteLimitPolicy:
        return self._route_policies.get(
            route_class,
            RouteLimitPolicy(
                rate_per_minute=self.default_rate_per_minute,
                burst=self.default_burst,
                max_concurrent=None,
            ),
        )

    async def acquire(
        self,
        tenant_id: str,
        route_class: str,
        principal_id: str | None = None,
    ) -> LimiterDecision:
        try:
            async with self._lock:
                now = self.clock()
                policy = self.get_policy(route_class)
                key = (tenant_id, route_class)

                state = self._states.get(key)
                if state is None:
                    state = _BucketState(
                        tokens=float(policy.burst),
                        last_refill=now,
                        active_concurrency=0,
                    )
                    self._states[key] = state
                else:
                    fill_rate = policy.rate_per_minute / 60.0
                    elapsed = max(0.0, now - state.last_refill)
                    state.tokens = min(float(policy.burst), state.tokens + elapsed * fill_rate)
                    state.last_refill = now

                # 1. Check in-flight concurrency quota
                if (
                    policy.max_concurrent is not None
                    and state.active_concurrency >= policy.max_concurrent
                ):
                    return LimiterDecision(
                        allowed=False,
                        retry_after_seconds=1.0,
                        remaining=0,
                        limit=policy.max_concurrent,
                        reason="concurrency_limit_exceeded",
                    )

                # 2. Check rate limit / token bucket
                if state.tokens < 1.0:
                    fill_rate = policy.rate_per_minute / 60.0
                    needed = 1.0 - state.tokens
                    retry_after = max(0.1, needed / fill_rate) if fill_rate > 0 else 60.0
                    return LimiterDecision(
                        allowed=False,
                        retry_after_seconds=retry_after,
                        remaining=0,
                        limit=policy.burst,
                        reason="rate_limit_exceeded",
                    )

                # Consume token and increment active concurrency if bounded
                state.tokens -= 1.0
                if policy.max_concurrent is not None:
                    state.active_concurrency += 1

                return LimiterDecision(
                    allowed=True,
                    retry_after_seconds=0.0,
                    remaining=int(state.tokens),
                    limit=policy.burst,
                    reason=None,
                )
        except Exception:
            if self.fail_closed:
                return LimiterDecision(
                    allowed=False,
                    retry_after_seconds=5.0,
                    remaining=0,
                    limit=0,
                    reason="limiter_error",
                )
            return LimiterDecision(
                allowed=True,
                retry_after_seconds=0.0,
                remaining=1,
                limit=1,
                reason=None,
            )

    async def release(
        self,
        tenant_id: str,
        route_class: str,
        principal_id: str | None = None,
    ) -> None:
        async with self._lock:
            policy = self.get_policy(route_class)
            if policy.max_concurrent is None:
                return
            key = (tenant_id, route_class)
            state = self._states.get(key)
            if state is not None and state.active_concurrency > 0:
                state.active_concurrency -= 1

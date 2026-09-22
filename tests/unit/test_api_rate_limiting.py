from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.api.rate_limiting import (
    ROUTE_CLASS_READ,
    ROUTE_CLASS_RUN_CREATE,
    ROUTE_CLASS_WRITE,
    InMemoryApiRateLimiter,
)
from algen_agent_runtime.config.settings import ApiRateLimitSettings, ApiSettings, AppSettings
from algen_agent_runtime.orchestration.container import Container
from algen_agent_runtime.types.contracts import RunState, RunStatus


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.current = start

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


class TestInMemoryApiRateLimiter:
    """Unit tests for single-node token bucket and concurrency quota limiter."""

    @pytest.mark.anyio
    async def test_under_and_at_limit_consumption(self) -> None:
        clock = FakeClock()
        limiter = InMemoryApiRateLimiter(
            default_rate_per_minute=60,  # 1 token / sec
            default_burst=3,
            clock=clock,
        )

        # 1st request consumes token
        d1 = await limiter.acquire("tenant-a", ROUTE_CLASS_READ)
        assert d1.allowed is True
        assert d1.remaining == 2

        # 2nd request
        d2 = await limiter.acquire("tenant-a", ROUTE_CLASS_READ)
        assert d2.allowed is True
        assert d2.remaining == 1

        # 3rd request (at burst limit)
        d3 = await limiter.acquire("tenant-a", ROUTE_CLASS_READ)
        assert d3.allowed is True
        assert d3.remaining == 0

        # 4th request (over limit)
        d4 = await limiter.acquire("tenant-a", ROUTE_CLASS_READ)
        assert d4.allowed is False
        assert d4.reason == "rate_limit_exceeded"
        assert d4.retry_after_seconds > 0

    @pytest.mark.anyio
    async def test_token_bucket_refill(self) -> None:
        clock = FakeClock()
        limiter = InMemoryApiRateLimiter(
            default_rate_per_minute=60,  # 1 token / sec
            default_burst=2,
            clock=clock,
        )

        # Exhaust 2 tokens
        assert (await limiter.acquire("tenant-a", ROUTE_CLASS_READ)).allowed is True
        assert (await limiter.acquire("tenant-a", ROUTE_CLASS_READ)).allowed is True
        assert (await limiter.acquire("tenant-a", ROUTE_CLASS_READ)).allowed is False

        # Advance clock by 1.5 seconds -> 1 token refilled
        clock.advance(1.5)
        d = await limiter.acquire("tenant-a", ROUTE_CLASS_READ)
        assert d.allowed is True
        assert d.remaining == 0

        # Second request immediately is rejected
        assert (await limiter.acquire("tenant-a", ROUTE_CLASS_READ)).allowed is False

        # Advance by 10 seconds -> refilled up to burst (2)
        clock.advance(10.0)
        d1 = await limiter.acquire("tenant-a", ROUTE_CLASS_READ)
        d2 = await limiter.acquire("tenant-a", ROUTE_CLASS_READ)
        assert d1.allowed is True
        assert d2.allowed is True
        assert (await limiter.acquire("tenant-a", ROUTE_CLASS_READ)).allowed is False

    @pytest.mark.anyio
    async def test_tenant_isolation(self) -> None:
        clock = FakeClock()
        limiter = InMemoryApiRateLimiter(
            default_rate_per_minute=60,
            default_burst=1,
            clock=clock,
        )

        # Tenant A exhausts quota
        assert (await limiter.acquire("tenant-a", ROUTE_CLASS_READ)).allowed is True
        assert (await limiter.acquire("tenant-a", ROUTE_CLASS_READ)).allowed is False

        # Tenant B has full quota and is unaffected
        d_b = await limiter.acquire("tenant-b", ROUTE_CLASS_READ)
        assert d_b.allowed is True
        assert d_b.remaining == 0
        assert (await limiter.acquire("tenant-b", ROUTE_CLASS_READ)).allowed is False

    @pytest.mark.anyio
    async def test_route_class_independence(self) -> None:
        clock = FakeClock()
        limiter = InMemoryApiRateLimiter(
            default_rate_per_minute=60,
            default_burst=1,
            clock=clock,
        )

        # Exhaust write quota
        assert (await limiter.acquire("tenant-a", ROUTE_CLASS_WRITE)).allowed is True
        assert (await limiter.acquire("tenant-a", ROUTE_CLASS_WRITE)).allowed is False

        # Read quota is independent and still available
        assert (await limiter.acquire("tenant-a", ROUTE_CLASS_READ)).allowed is True
        assert (await limiter.acquire("tenant-a", ROUTE_CLASS_READ)).allowed is False

    @pytest.mark.anyio
    async def test_concurrency_quota_bounding_and_release(self) -> None:
        clock = FakeClock()
        limiter = InMemoryApiRateLimiter(
            default_rate_per_minute=600,
            default_burst=100,
            max_concurrent_runs_per_tenant=2,
            clock=clock,
        )

        # Acquire 2 concurrent runs
        d1 = await limiter.acquire("tenant-a", ROUTE_CLASS_RUN_CREATE)
        d2 = await limiter.acquire("tenant-a", ROUTE_CLASS_RUN_CREATE)
        assert d1.allowed is True
        assert d2.allowed is True

        # 3rd concurrent run exceeds concurrency quota
        d3 = await limiter.acquire("tenant-a", ROUTE_CLASS_RUN_CREATE)
        assert d3.allowed is False
        assert d3.reason == "concurrency_limit_exceeded"
        assert d3.retry_after_seconds == 1.0

        # Release one run
        await limiter.release("tenant-a", ROUTE_CLASS_RUN_CREATE)

        # Now acquire succeeds
        d4 = await limiter.acquire("tenant-a", ROUTE_CLASS_RUN_CREATE)
        assert d4.allowed is True

        # Releasing more than acquired does not cause negative concurrency
        await limiter.release("tenant-a", ROUTE_CLASS_RUN_CREATE)
        await limiter.release("tenant-a", ROUTE_CLASS_RUN_CREATE)
        await limiter.release("tenant-a", ROUTE_CLASS_RUN_CREATE)

    @pytest.mark.anyio
    async def test_fail_closed_on_error(self) -> None:
        def broken_clock() -> float:
            raise RuntimeError("clock malfunction")

        limiter = InMemoryApiRateLimiter(clock=broken_clock, fail_closed=True)
        decision = await limiter.acquire("tenant-a", ROUTE_CLASS_READ)
        assert decision.allowed is False
        assert decision.reason == "limiter_error"

        limiter_fail_open = InMemoryApiRateLimiter(clock=broken_clock, fail_closed=False)
        decision_open = await limiter_fail_open.acquire("tenant-a", ROUTE_CLASS_READ)
        assert decision_open.allowed is True


class TestFastAPIAdmissionRateLimiting:
    """Integration tests for FastAPI admission rate limiting middleware."""

    @pytest.fixture
    def test_settings(self) -> AppSettings:
        return AppSettings(
            api=ApiSettings(
                rate_limiting=ApiRateLimitSettings(
                    enabled=True,
                    default_rate_per_minute=60,
                    default_burst=2,
                    max_concurrent_runs_per_tenant=1,
                )
            )
        )

    @pytest.mark.anyio
    async def test_rate_limit_exceeded_returns_429(self, test_settings: AppSettings) -> None:
        app = create_app(test_settings)
        headers = {
            "x-tenant-id": "tenant-test",
            "x-user-id": "user-test",
            "x-scopes": "runs:read runs:write",
        }

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # First 2 requests succeed (within burst of 2)
            # Note: /health/live is exempt, so let's hit a protected endpoint like /v1/runs/missing
            r1 = await client.get("/v1/runs/run-1", headers=headers)
            assert r1.status_code == 404  # passed admission, returned 404 from store

            r2 = await client.get("/v1/runs/run-2", headers=headers)
            assert r2.status_code == 404

            # 3rd request exceeds burst limit -> 429
            r3 = await client.get("/v1/runs/run-3", headers=headers)
            assert r3.status_code == 429
            assert "Retry-After" in r3.headers
            assert int(r3.headers["Retry-After"]) >= 1

            body = r3.json()
            assert body["code"] == "RATE_LIMIT_EXCEEDED"
            assert body["tenant_id"] == "tenant-test"
            assert body["route_class"] == "read"
            assert "Rate limit exceeded" in body["detail"]

    @pytest.mark.anyio
    async def test_health_routes_exempt_from_rate_limits(self, test_settings: AppSettings) -> None:
        app = create_app(test_settings)
        headers = {
            "x-tenant-id": "tenant-test",
            "x-user-id": "user-test",
            "x-scopes": "runs:read",
        }

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Exhaust rate limit
            await client.get("/v1/runs/run-1", headers=headers)
            await client.get("/v1/runs/run-2", headers=headers)
            blocked = await client.get("/v1/runs/run-3", headers=headers)
            assert blocked.status_code == 429

            # /health/live and /health/ready are exempt and never blocked
            live_res = await client.get("/health/live")
            assert live_res.status_code == 200
            assert live_res.json() == {"status": "ok"}

            ready_res = await client.get("/health/ready")
            assert ready_res.status_code in {200, 503}

    @pytest.mark.anyio
    async def test_concurrency_quota_on_run_create(self, test_settings: AppSettings) -> None:
        # Create a container where runtime.start waits on an event
        container = MagicMock(spec=Container)
        container.observability = MagicMock()
        container.observability.start = MagicMock()
        container.astart = AsyncMock()
        container.aclose = AsyncMock()
        container.resource_health = AsyncMock(return_value={})
        container.rate_limiter = None

        started_event = asyncio.Event()
        finish_event = asyncio.Event()

        fake_run_state = MagicMock()
        fake_run_state.id = "run-1"
        fake_run_state.session_id = "session-1"
        fake_run_state.status = RunStatus.RECEIVED

        async def slow_start(req: Any) -> RunState:
            started_event.set()
            await finish_event.wait()
            return fake_run_state

        mock_runtime = MagicMock()
        mock_runtime.start = AsyncMock(side_effect=slow_start)
        mock_runtime.router = MagicMock()
        mock_runtime.router.providers = MagicMock(return_value=[])
        container.runtime = mock_runtime

        app = create_app(test_settings, container=container)
        headers = {
            "x-tenant-id": "tenant-conc",
            "x-user-id": "user-conc",
            "x-scopes": "runs:write",
        }
        body = {"agent": "test-agent", "input": "hello"}

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Launch first run in background
            task1 = asyncio.create_task(client.post("/v1/runs", json=body, headers=headers))
            await started_event.wait()

            # Second concurrent run for same tenant exceeds max_concurrent_runs_per_tenant=1
            r2 = await client.post("/v1/runs", json=body, headers=headers)
            assert r2.status_code == 429
            body2 = r2.json()
            assert body2["code"] == "CONCURRENCY_LIMIT_EXCEEDED"
            assert body2["route_class"] == "run_create"

            # Finish first run
            finish_event.set()
            r1 = await task1
            assert r1.status_code == 202

    @pytest.mark.anyio
    async def test_spoofed_headers_cannot_bypass_tenant_limit(
        self, test_settings: AppSettings
    ) -> None:
        app = create_app(test_settings)
        headers = {
            "x-tenant-id": "tenant-fixed",
            "x-user-id": "user-1",
            "x-scopes": "runs:read",
        }

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # 2 requests consume the burst
            await client.get("/v1/runs/run-1", headers=headers)
            await client.get("/v1/runs/run-2", headers=headers)

            # Attempt to bypass using fake proxy headers
            spoofed_headers = {
                **headers,
                "x-forwarded-for": "198.51.100.1, 10.0.0.1",
                "client-ip": "203.0.113.195",
                "x-real-ip": "192.0.2.1",
            }
            res = await client.get("/v1/runs/run-3", headers=spoofed_headers)
            assert res.status_code == 429
            assert res.json()["tenant_id"] == "tenant-fixed"

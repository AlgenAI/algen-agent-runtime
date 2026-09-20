from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from time import monotonic
from typing import Any

import structlog
from opentelemetry import metrics, trace

from algen_agent_runtime.cache.contracts import CacheContext, CachePolicy, CacheScope, CacheStore
from algen_agent_runtime.observability.trace_levels import TraceLevel, TraceLevelTracer


class CacheService:
    """Fail-open scoped cache with stampede protection and content-free telemetry."""

    def __init__(
        self,
        store: CacheStore,
        policies: Mapping[str, CachePolicy] | None = None,
        *,
        key_secret: str | None = None,
        telemetry_trace_level: TraceLevel = "detailed",
    ) -> None:
        self.store = store
        self._policies = dict(policies or {})
        self._secret = key_secret.encode() if key_secret else None
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self._inflight_lock = asyncio.Lock()
        self._tracer = TraceLevelTracer(
            trace.get_tracer("algen_agent_runtime.cache"), telemetry_trace_level
        )
        meter = metrics.get_meter("algen_agent_runtime.cache")
        self._operations = meter.create_counter("algen_agent_runtime.cache.operations")
        self._latency = meter.create_histogram(
            "algen_agent_runtime.cache.operation.duration", unit="ms"
        )
        self._logger = structlog.get_logger("algen_agent_runtime.cache")

    def policy(self, name: str) -> CachePolicy:
        return self._policies.get(name, CachePolicy())

    async def get_json(
        self, policy_name: str, namespace: str, material: Any, context: CacheContext
    ) -> Any | None:
        policy = self.policy(policy_name)
        if not policy.enabled:
            return None
        key = self._key(policy_name, namespace, material, context, policy)
        attributes = self._attributes(policy_name, namespace, policy)
        started = monotonic()
        with self._tracer.start_as_current_span("cache.get", attributes=attributes) as span:
            try:
                encoded = await self.store.get(key)
                hit = encoded is not None
                span.set_attribute("cache.hit", hit)
                self._operations.add(1, {**attributes, "cache.outcome": "hit" if hit else "miss"})
                return json.loads(encoded) if encoded is not None else None
            except Exception as exc:
                span.record_exception(exc)
                span.set_attribute("cache.outcome", "error")
                self._operations.add(1, {**attributes, "cache.outcome": "error"})
                self._logger.warning(
                    "cache_get_failed",
                    backend=self.store.backend_name,
                    namespace=self._namespace(namespace),
                    error_type=type(exc).__name__,
                )
                return None
            finally:
                self._latency.record((monotonic() - started) * 1000, attributes)

    async def set_json(
        self,
        policy_name: str,
        namespace: str,
        material: Any,
        value: Any,
        context: CacheContext,
        *,
        tags: tuple[str, ...] = (),
    ) -> bool:
        policy = self.policy(policy_name)
        if not policy.enabled:
            return False
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
        attributes = self._attributes(policy_name, namespace, policy)
        if len(encoded) > policy.maximum_value_bytes:
            self._operations.add(1, {**attributes, "cache.outcome": "oversize"})
            return False
        key = self._key(policy_name, namespace, material, context, policy)
        safe_tags = tuple(self._digest(tag) for tag in tags)
        started = monotonic()
        with self._tracer.start_as_current_span("cache.set", attributes=attributes) as span:
            span.set_attribute("cache.value_size_bytes", len(encoded))
            try:
                await self.store.set(key, encoded, policy.ttl_seconds, safe_tags)
                self._operations.add(1, {**attributes, "cache.outcome": "stored"})
                return True
            except Exception as exc:
                span.record_exception(exc)
                span.set_attribute("cache.outcome", "error")
                self._operations.add(1, {**attributes, "cache.outcome": "error"})
                self._logger.warning(
                    "cache_set_failed",
                    backend=self.store.backend_name,
                    namespace=self._namespace(namespace),
                    error_type=type(exc).__name__,
                )
                return False
            finally:
                self._latency.record((monotonic() - started) * 1000, attributes)

    async def get_or_set_json(
        self,
        policy_name: str,
        namespace: str,
        material: Any,
        context: CacheContext,
        loader: Callable[[], Awaitable[Any]],
        *,
        tags: tuple[str, ...] = (),
    ) -> tuple[Any, bool]:
        cached = await self.get_json(policy_name, namespace, material, context)
        if cached is not None:
            return cached, True
        policy = self.policy(policy_name)
        if not policy.enabled:
            return await loader(), False
        key = self._key(policy_name, namespace, material, context, policy)
        owner = False
        async with self._inflight_lock:
            future = self._inflight.get(key)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._inflight[key] = future
                owner = True
        if not owner:
            return await asyncio.shield(future), True
        try:
            value = await loader()
            await self.set_json(policy_name, namespace, material, value, context, tags=tags)
            future.set_result(value)
            return value, False
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
                future.exception()
            raise
        finally:
            async with self._inflight_lock:
                self._inflight.pop(key, None)

    async def invalidate_tags(self, *tags: str) -> int:
        safe_tags = tuple(self._digest(tag) for tag in tags)
        with self._tracer.start_as_current_span(
            "cache.invalidate", attributes={"cache.backend": self.store.backend_name}
        ) as span:
            try:
                count = await self.store.invalidate_tags(safe_tags)
                span.set_attribute("cache.invalidated_count", count)
                return count
            except Exception as exc:
                span.record_exception(exc)
                self._logger.warning("cache_invalidation_failed", error_type=type(exc).__name__)
                return 0

    def _key(
        self,
        policy_name: str,
        namespace: str,
        material: Any,
        context: CacheContext,
        policy: CachePolicy,
    ) -> str:
        scope = self._scope_material(context, policy.scope)
        canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
        return ":".join(
            (
                "v1",
                self._namespace(policy_name),
                self._namespace(namespace),
                self._digest(scope),
                self._digest(canonical),
            )
        )

    def _scope_material(self, context: CacheContext, scope: CacheScope) -> str:
        parts: list[str] = []
        if scope in {CacheScope.TENANT, CacheScope.USER, CacheScope.SESSION, CacheScope.RUN}:
            if not context.tenant_id:
                raise ValueError(f"cache scope {scope.value} requires tenant_id")
            parts.append(context.tenant_id)
        if scope in {CacheScope.USER, CacheScope.SESSION, CacheScope.RUN}:
            if not context.user_id:
                raise ValueError(f"cache scope {scope.value} requires user_id")
            parts.append(context.user_id)
        if scope == CacheScope.SESSION:
            if not context.session_id:
                raise ValueError("cache scope session requires session_id")
            parts.append(context.session_id)
        if scope == CacheScope.RUN:
            if not context.run_id:
                raise ValueError("cache scope run requires run_id")
            parts.append(context.run_id)
        if context.authorization_fingerprint:
            parts.append(context.authorization_fingerprint)
        return "\x1f".join(parts) or "global"

    def _digest(self, value: str) -> str:
        raw = value.encode()
        return (
            hmac.new(self._secret, raw, hashlib.sha256).hexdigest()
            if self._secret
            else hashlib.sha256(raw).hexdigest()
        )

    @staticmethod
    def _namespace(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]", "_", value)[:96]

    def _attributes(self, policy_name: str, namespace: str, policy: CachePolicy) -> dict[str, str]:
        return {
            "cache.backend": self.store.backend_name,
            "cache.policy": policy_name,
            "cache.namespace": self._namespace(namespace),
            "cache.scope": policy.scope.value,
        }

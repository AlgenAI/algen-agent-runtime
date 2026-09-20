from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from opentelemetry import trace

from algen_agent_runtime.cache import CacheContext, CacheService
from algen_agent_runtime.exceptions.errors import PolicyDeniedError
from algen_agent_runtime.governance.contracts import GovernedQueryRequest, QueryEstimate
from algen_agent_runtime.governance.engine import QueryGovernanceEngine, query_fingerprint


class QueryBackend(Protocol):
    async def execute(self, sql: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]: ...


QueryEstimator = Callable[[str, tuple[Any, ...]], Awaitable[QueryEstimate]]
AuditSink = Callable[[dict[str, Any]], Awaitable[None]]


class GovernedQueryExecutor:
    """Reusable policy, quota, concurrency, fingerprint, cache and audit wrapper."""

    def __init__(
        self,
        backend: QueryBackend,
        governance: QueryGovernanceEngine,
        *,
        estimator: QueryEstimator | None = None,
        cache: CacheService | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self._backend = backend
        self._governance = governance
        self._estimator = estimator
        self._cache = cache
        self._audit = audit
        self._pools: defaultdict[tuple[str, str], asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(governance.policy.maximum_concurrency)
        )
        self._tracer = trace.get_tracer("algen_agent_runtime.governance")

    async def execute(
        self,
        sql: str,
        parameters: tuple[Any, ...],
        request: GovernedQueryRequest,
        *,
        semantic_digest: str | None = None,
        authorization_fingerprint: str | None = None,
    ) -> tuple[list[dict[str, Any]], str, bool]:
        fingerprint = query_fingerprint(
            sql,
            parameters,
            semantic_digest=semantic_digest,
            authorization_fingerprint=authorization_fingerprint,
        )
        estimate = (
            await self._estimator(sql, parameters)
            if self._estimator is not None
            else request.estimate
        )
        governed = request.model_copy(
            update={"estimate": estimate, "query_fingerprint": fingerprint}
        )
        decision = self._governance.enforce(governed)
        await self._audit_event(governed, decision.decision.value)

        async def load() -> list[dict[str, Any]]:
            pool = self._pools[(request.tenant_id, request.purpose)]
            async with pool:
                async with asyncio.timeout(self._governance.policy.maximum_compute_seconds):
                    return await self._backend.execute(sql, parameters)

        attributes = {
            "query.fingerprint": fingerprint,
            "query.purpose": request.purpose,
            "tenant.id": request.tenant_id,
        }
        with self._tracer.start_as_current_span("governed_query.execute", attributes=attributes):
            if self._cache is None:
                rows = await load()
                self._validate_actual_rows(rows)
                return rows, fingerprint, False
            value, hit = await self._cache.get_or_set_json(
                "query_results",
                "governed_query",
                {"fingerprint": fingerprint},
                CacheContext(
                    tenant_id=request.tenant_id,
                    user_id=request.user_id,
                    authorization_fingerprint=authorization_fingerprint,
                ),
                load,
                tags=(*(f"source:{source}" for source in request.source_ids),),
            )
            rows = [dict(item) for item in value]
            self._validate_actual_rows(rows)
            return rows, fingerprint, hit

    def _validate_actual_rows(self, rows: list[dict[str, Any]]) -> None:
        if len(rows) > self._governance.policy.maximum_rows:
            raise PolicyDeniedError(
                "governed query result exceeded the configured actual row quota"
            )

    async def _audit_event(self, request: GovernedQueryRequest, outcome: str) -> None:
        if self._audit is None:
            return
        await self._audit(
            {
                "action": "governed_query",
                "outcome": outcome,
                "tenant_id": request.tenant_id,
                "user_id": request.user_id,
                "purpose": request.purpose,
                "source_ids": request.source_ids,
                "query_fingerprint": request.query_fingerprint,
                "retention_days": self._governance.policy.audit_retention_days,
            }
        )

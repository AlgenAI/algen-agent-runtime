"""What ``/health/ready`` reports: every dependency, whether it is required, and why it is down.

A dependency is named ``<kind>.<name>`` (``store.run``, ``cache.redis``, ``provider.openai``,
``retriever.docs``, ``mcp.crm``, ``telemetry.otlp``, ``resource.<TypeName>``) and has a status:

- ``up``: its probe passed (or it is in memory);
- ``down``: its probe failed or timed out, or it failed at startup, with a short reason;
- ``unknown``: it cannot be probed (an OTLP exporter, a retriever without ``health()``); it is
  reported but never fails readiness.

Readiness fails only when a required dependency is down, when the model-provider rule
(``runtime.readiness.model_providers``) is not met, or while the container drains. Reasons are
content-minimal: an exception type, or a short message with URLs, DSNs and credentials removed.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from algen_agent_runtime.config.settings import AppSettings, ReadinessSettings

Status = Literal["up", "down", "unknown"]
Probe = Callable[[], Awaitable[Any] | Any]

_MAX_REASON = 160
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+")
_SECRET = re.compile(
    r"(?i)\b(bearer|basic|token|api[_-]?key|secret|password|passwd|pwd|authorization)"
    r"(\s*[:=]\s*|\s+)(?:(?:bearer|basic)\s+)?\S+"
)


def content_minimal_reason(text: str) -> str:
    """A short reason that is safe to return from an unauthenticated probe."""
    reason = _URL.sub("<redacted-url>", text)
    reason = _SECRET.sub(lambda match: f"{match.group(1)}=<redacted>", reason)
    reason = " ".join(reason.split())
    if len(reason) > _MAX_REASON:
        reason = reason[: _MAX_REASON - 1] + "…"
    return reason or "unavailable"


def in_memory() -> bool:
    """The probe of an in-memory store: always up."""
    return True


def resource_probe(resource: object) -> Probe | None:
    """A resource's ``health()`` or ``ping()``, if it has one."""
    probe = getattr(resource, "health", None) or getattr(resource, "ping", None)
    return probe if callable(probe) else None


@dataclass(frozen=True)
class DeclaredDependency:
    """A dependency known when the container was built: a store, the cache, an exporter.

    It is probed through ``resource`` (its ``health()`` or ``ping()``) when given, otherwise
    through ``probe``; with neither its status is ``unknown``. A resource that backs a declared
    dependency is not listed again as ``resource.<TypeName>``.
    """

    name: str
    required_by_default: bool
    backend: str | None = None
    resource: object | None = None
    probe: Probe | None = None


@dataclass
class ReadinessChecker:
    """Builds the readiness report of a container from its declared and live dependencies."""

    settings: ReadinessSettings = field(default_factory=ReadinessSettings)
    declared: list[DeclaredDependency] = field(default_factory=list)

    def declare(
        self,
        name: str,
        *,
        required: bool,
        backend: str | None = None,
        resource: object | None = None,
        probe: Probe | None = None,
    ) -> None:
        self.declared.append(DeclaredDependency(name, required, backend, resource, probe))

    async def report(self, container: Any) -> tuple[bool, dict[str, Any]]:
        """``(ready, body)`` for ``/health/ready``; ``body`` keeps the earlier keys."""
        phase = container.lifecycle.phase
        degraded = {
            name: content_minimal_reason(reason)
            for name, reason in container.degraded_dependencies.items()
        }
        if phase in {"draining", "stopped"}:
            # Probing now could reopen connection pools that are being closed.
            return False, {
                "status": "draining",
                "ready": False,
                "lifecycle": phase,
                "providers": {},
                "stores": {},
                "dependencies": {},
                "degraded_dependencies": degraded,
                "failing": [],
            }

        # Each resource is probed once, whatever reports it.
        resources: dict[int, object] = {}
        for resource in container.resources:
            if resource_probe(resource) is not None:
                resources[id(resource)] = resource
        for item in self.declared:
            if item.resource is not None and resource_probe(item.resource) is not None:
                resources[id(item.resource)] = item.resource
        probes: dict[str, Probe | None] = {
            f"#{key}": resource_probe(resource) for key, resource in resources.items()
        }

        entries: dict[str, dict[str, Any]] = {}
        sources: dict[str, str] = {}

        def add(
            name: str,
            required: bool,
            *,
            probe: Probe | None = None,
            shared: str | None = None,
            backend: str | None = None,
        ) -> None:
            """``shared`` names the probe of a resource that several entries report."""
            entries[name] = {
                "kind": name.partition(".")[0],
                "required": self._required(name, required),
                **({"backend": backend} if backend else {}),
            }
            sources[name] = shared or name
            if shared is None:
                probes[name] = probe

        covered: set[int] = set()
        for item in self.declared:
            if item.resource is not None and id(item.resource) in resources:
                covered.add(id(item.resource))
                add(
                    item.name,
                    item.required_by_default,
                    shared=f"#{id(item.resource)}",
                    backend=item.backend,
                )
            else:
                add(item.name, item.required_by_default, probe=item.probe, backend=item.backend)
        providers = list(container.runtime.router.providers())
        for provider in providers:
            add(
                f"provider.{provider.provider_id}",
                self.settings.model_providers == "all",
                probe=provider.health,
            )
        for resource in container.resources:
            if id(resource) in resources and id(resource) not in covered:
                add(
                    f"resource.{type(resource).__name__}",
                    self.settings.require_durable_stores,
                    shared=f"#{id(resource)}",
                )
        for name in container.retrievers.list():
            add(f"retriever.{name}", False, probe=resource_probe(container.retrievers.get(name)))
        if container.mcp is not None:
            from algen_agent_runtime.mcp.contracts import oauth_config

            for server in container.mcp.server_names:
                config = container.mcp.get_server(server)
                # An OAuth server is connected per tenant or user, not per process.
                required = config.required and oauth_config(config) is None
                add(f"mcp.{server}", required, probe=in_memory)

        results = dict(
            zip(
                probes,
                await asyncio.gather(*(self._probe(probe) for probe in probes.values())),
                strict=True,
            )
        )
        for name, entry in entries.items():
            status, reason = results[sources[name]]
            entry["status"] = status
            if reason:
                entry["reason"] = reason
        for name, reason in degraded.items():
            entry = entries.setdefault(
                name, {"kind": name.partition(".")[0], "required": self._required(name, False)}
            )
            entry["status"] = "down"
            entry["reason"] = reason
        for name in self.settings.required:
            if name not in entries:
                entries[name] = {
                    "kind": name.partition(".")[0],
                    "required": True,
                    "status": "down",
                    "reason": "not registered",
                }

        failing = sorted(
            name
            for name, entry in entries.items()
            if entry["required"] and entry["status"] == "down"
        )
        provider_names = [f"provider.{provider.provider_id}" for provider in providers]
        if (
            self.settings.model_providers == "any"
            and provider_names
            and not any(entries[name]["status"] == "up" for name in provider_names)
        ):
            failing.append("provider.*")
        ready = not failing
        everything_up = all(entry["status"] != "down" for entry in entries.values())
        return ready, {
            "status": "ready" if ready and everything_up else "degraded",
            "ready": ready,
            "lifecycle": phase,
            "providers": {
                provider.provider_id: entries[f"provider.{provider.provider_id}"]["status"] == "up"
                for provider in providers
            },
            # Earlier releases reported each health-capable resource by type name.
            "stores": {
                type(resource).__name__: results[f"#{id(resource)}"][0] == "up"
                for resource in container.resources
                if id(resource) in resources
            },
            "dependencies": dict(sorted(entries.items())),
            "degraded_dependencies": degraded,
            "failing": failing,
        }

    def _required(self, name: str, default: bool) -> bool:
        if name in self.settings.required:
            return True
        if name in self.settings.optional:
            return False
        return default

    async def _probe(self, probe: Probe | None) -> tuple[Status, str | None]:
        if probe is None:
            return "unknown", "not probeable"
        try:
            async with asyncio.timeout(self.settings.probe_timeout_seconds):
                result = probe()
                if inspect.isawaitable(result):
                    result = await result
        except TimeoutError:
            return "down", "probe timed out"
        except Exception as exc:
            return "down", type(exc).__name__
        if getattr(result, "healthy", result):
            return "up", None
        return "down", "probe failed"


def declare_dependencies(
    settings: AppSettings,
    *,
    database: object | None,
    redis_client: object | None,
    cache_redis_client: object | None,
    artifacts: object,
    observability: object,
) -> ReadinessChecker:
    """The dependencies a container built from ``settings`` has, as ``build_container`` wires them."""
    readiness = settings.runtime.readiness
    checker = ReadinessChecker(readiness)
    storage = settings.storage
    stores: dict[str, str] = {
        "run": storage.run_store,
        "event": storage.event_store,
        "approval": storage.approval_store,
        "audit": storage.audit_store,
        "artifact": storage.artifact_store,
        "workflow": storage.workflow_store,
        "tool_execution": storage.tool_execution_store,
        "conversation": storage.conversation_store,
        "memory": storage.memory_store,
        "learned_memory": storage.learned_memory_store,
        "analytical_graph": settings.analytical_execution.graph_store,
    }
    if settings.distributed_execution.enabled:
        stores["work_queue"] = settings.distributed_execution.queue_backend
    if settings.mcp_servers:
        stores["mcp_oauth_token"] = storage.mcp_oauth_token_store
    backing = {"postgres": database, "redis": redis_client}
    for kind, backend in stores.items():
        resource = artifacts if backend == "s3" else backing.get(backend)
        checker.declare(
            f"store.{kind}",
            required=readiness.require_durable_stores,
            backend=backend,
            resource=resource,
            probe=in_memory if backend == "memory" else None,
        )
    if settings.cache.backend == "redis":
        checker.declare("cache.redis", required=False, backend="redis", resource=cache_redis_client)
    telemetry = settings.telemetry
    if telemetry.enabled and telemetry.otlp_endpoint:
        # The OTLP exporter has no health check; it is reported as unknown.
        checker.declare("telemetry.otlp", required=False)
    if telemetry.enabled and telemetry.traccia.enabled:
        checker.declare(
            "telemetry.traccia", required=False, probe=getattr(observability, "health", None)
        )
    return checker

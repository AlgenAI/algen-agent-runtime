from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from algen_agent_runtime.config.settings import EnvironmentSecretProvider
from algen_agent_runtime.exceptions.errors import ConfigurationError, ConflictError

SecretResolver = Callable[[str], Awaitable[str]]

if TYPE_CHECKING:
    from algen_agent_runtime.analytics import AnalyticalNodeRegistry
    from algen_agent_runtime.analytics.contracts import AnalyticalNodeHandler
    from algen_agent_runtime.cache import CacheService
    from algen_agent_runtime.mcp.contracts import MCPServerConfigBase
    from algen_agent_runtime.models.base import ModelRouter
    from algen_agent_runtime.policies.engine import CompositePolicyEngine
    from algen_agent_runtime.retrieval.registry import RetrieverRegistry
    from algen_agent_runtime.types.interfaces import MemoryStore, ModelProvider, VectorStore


ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ComponentSources:
    """Which plugin registered each component, by kind and name, and the env vars it declared."""

    def __init__(self) -> None:
        self._sources: dict[str, dict[str, str]] = {}
        self.env: dict[str, set[str]] = {}
        self.current: str | None = None

    def record_env(self, names: Iterable[str]) -> None:
        if self.current is not None:
            self.env.setdefault(self.current, set()).update(names)

    def record(self, kind: str, name: str) -> None:
        if self.current is not None:
            self._sources.setdefault(kind, {})[name] = self.current

    def as_dict(self) -> dict[str, dict[str, str]]:
        return {kind: dict(names) for kind, names in self._sources.items()}


class GuardedRegistry:
    """Adds components to a registry and refuses a name that is already taken."""

    def __init__(
        self,
        kind: str,
        sources: ComponentSources,
        *,
        names: Callable[[], Iterable[str]],
        key: Callable[..., str],
        add: Callable[..., None],
    ) -> None:
        self._kind = kind
        self._sources = sources
        self._names = names
        self._key = key
        self._add = add

    def register(self, *args: Any, **kwargs: Any) -> None:
        name = self._key(*args, **kwargs)
        if name in set(self._names()):
            raise ConflictError(f"{self._kind} {name!r} is already registered")
        self._add(*args, **kwargs)
        self._sources.record(self._kind, name)


class ProviderRegistrar:
    """Registers model providers on the router under a unique registration id."""

    def __init__(
        self,
        router: ModelRouter,
        sources: ComponentSources,
        reserved: frozenset[str] = frozenset(),
    ) -> None:
        self._router = router
        self._sources = sources
        self._reserved = reserved

    def register(
        self,
        provider: ModelProvider,
        requests_per_minute: int = 600,
        cost_per_1k_input: float = 0,
        cost_per_1k_output: float = 0,
        is_local: bool = False,
        **kwargs: Any,
    ) -> None:
        name = kwargs.get("registration_id") or provider.provider_id
        if name in self._reserved:
            raise ConflictError(f"provider {name!r} is already registered")
        try:
            self._router.register_provider(
                provider,
                requests_per_minute,
                cost_per_1k_input,
                cost_per_1k_output,
                is_local,
                **kwargs,
            )
        except ValueError as exc:
            raise ConflictError(f"provider {name!r} is already registered") from exc
        self._sources.record("providers", name)


class MCPServerRegistrar:
    """Collects MCP server configurations; the container connects to them at start."""

    def __init__(self, servers: list[MCPServerConfigBase], sources: ComponentSources) -> None:
        self._servers = servers
        self._sources = sources

    def register(self, config: MCPServerConfigBase) -> None:
        if any(server.name == config.name for server in self._servers):
            raise ConflictError(f"MCP server {config.name!r} is already declared")
        self._servers.append(config)
        self._sources.record("mcp_servers", config.name)


class AnalyticalNodeRegistrar:
    """Supplies the handler for an analytical node kind that has none yet.

    The Runtime handles ``join_results``, ``compare_periods``, ``calculate``, ``rank``,
    ``verify`` and ``compose`` itself; ``semantic_query`` and ``model_call`` need the
    deployment's handler, because only it knows its data source and model.
    """

    def __init__(self, registry: AnalyticalNodeRegistry, sources: ComponentSources) -> None:
        self._registry = registry
        self._sources = sources

    def register(self, kind: str, handler: AnalyticalNodeHandler) -> None:
        from algen_agent_runtime.analytics import AnalyticalNodeKind

        try:
            node_kind = AnalyticalNodeKind(kind)
        except ValueError as exc:
            known = ", ".join(item.value for item in AnalyticalNodeKind)
            raise ConfigurationError(
                f"unknown analytical node kind {kind!r}; expected one of: {known}"
            ) from exc
        if node_kind in self._registry.kinds():
            raise ConflictError(f"analytical_nodes {node_kind.value!r} is already registered")
        self._registry.register(node_kind, handler)
        self._sources.record("analytical_nodes", node_kind.value)


class RetrieverRegistrar:
    """Registers a search store and the ``retrieval.<name>`` context builder that reads it."""

    def __init__(
        self,
        retrievers: RetrieverRegistry,
        contexts: Any,
        memory: MemoryStore,
        cache: CacheService | None,
        sources: ComponentSources,
    ) -> None:
        self._retrievers = retrievers
        self._contexts = contexts
        self._memory = memory
        self._cache = cache
        self._sources = sources

    def register(self, name: str, store: VectorStore, **options: Any) -> None:
        """``options`` are ``RetrievalContextBuilder``'s: ``mode``, ``limit``,
        ``minimum_score``, ``keyword_weight``, ``vector_weight``, ``filters`` and the
        ``max_*`` bounds."""
        from algen_agent_runtime.context.builder import RetrievalContextBuilder

        builder = f"retrieval.{name}"
        if name in self._retrievers.list():
            raise ConflictError(f"retrievers {name!r} is already registered")
        if builder in self._contexts.names():
            raise ConflictError(f"context_builders {builder!r} is already registered")
        context_builder = RetrievalContextBuilder(
            builder, self._memory, store, cache=self._cache, **options
        )
        self._retrievers.register(name, store)
        self._contexts.register(context_builder)
        self._sources.record("retrievers", name)
        self._sources.record("context_builders", builder)


def _versioned_add(add: Callable[..., None], kind: str) -> Callable[..., None]:
    def guarded(*args: Any, **kwargs: Any) -> None:
        try:
            add(*args, **kwargs)
        except ConfigurationError as exc:
            raise ConflictError(f"{kind}: {exc}") from exc

    return guarded


class RuntimePluginContext:
    """The ``PluginContext`` that ``build_container`` passes to each declared plugin."""

    def __init__(
        self,
        *,
        tools: Any,
        planners: Any,
        contexts: Any,
        verifiers: Any,
        composers: Any,
        semantics: Any,
        methods: Any,
        model_services: Any,
        router: ModelRouter,
        mcp_servers: list[MCPServerConfigBase],
        policies: CompositePolicyEngine,
        analytical_nodes: AnalyticalNodeRegistry,
        retrievers: RetrieverRegistry,
        memory: MemoryStore,
        cache: CacheService | None = None,
        reserved_providers: frozenset[str] = frozenset(),
        secrets: SecretResolver | None = None,
    ) -> None:
        self.sources = ComponentSources()
        self.secrets: SecretResolver = secrets or EnvironmentSecretProvider().get
        self.resources: list[object] = []
        self.tools = GuardedRegistry(
            "tools",
            self.sources,
            names=lambda: (tool.definition.name for tool in tools.list()),
            key=lambda tool: tool.definition.name,
            add=tools.register,
        )
        self.planners = self._named("planners", planners)
        self.contexts = self._named("context_builders", contexts)
        self.verifiers = self._named("verifiers", verifiers)
        self.composers = self._named("response_composers", composers)
        self.semantics = GuardedRegistry(
            "semantic_layers",
            self.sources,
            names=semantics.list,
            key=lambda layer: layer.definition.name,
            add=semantics.register,
        )
        self.methods = GuardedRegistry(
            "methods",
            self.sources,
            names=tuple,
            key=lambda manifest, implementation: f"{manifest.name}@{manifest.version}",
            add=_versioned_add(methods.register, "analytical method"),
        )
        self.model_services = GuardedRegistry(
            "model_services",
            self.sources,
            names=tuple,
            key=lambda service: f"{service.manifest.name}@{service.manifest.version}",
            add=_versioned_add(model_services.register, "model service"),
        )
        self.providers = ProviderRegistrar(router, self.sources, reserved_providers)
        self.mcp_servers = MCPServerRegistrar(mcp_servers, self.sources)
        self.policies = GuardedRegistry(
            "policies",
            self.sources,
            names=policies.list,
            key=lambda policy: policy.name,
            add=policies.register,
        )
        self.analytical_nodes = AnalyticalNodeRegistrar(analytical_nodes, self.sources)
        self.retrievers = RetrieverRegistrar(retrievers, contexts, memory, cache, self.sources)

    def _named(self, kind: str, registry: Any) -> GuardedRegistry:
        return GuardedRegistry(
            kind,
            self.sources,
            names=registry.names,
            key=lambda component: component.name,
            add=registry.register,
        )

    def declare_env(self, *names: str) -> None:
        """Environment variable names the registering plugin's components read."""
        for name in names:
            if not ENV_NAME.match(name):
                raise ConfigurationError(f"invalid environment variable name {name!r}")
        self.sources.record_env(names)

    def add_resource(self, resource: object) -> None:
        """Hand a shared client to the container, which starts and closes it with itself."""
        self.resources.append(resource)

    @contextmanager
    def registering(self, plugin: str) -> Iterator[None]:
        self.sources.current = plugin
        try:
            yield
        finally:
            self.sources.current = None

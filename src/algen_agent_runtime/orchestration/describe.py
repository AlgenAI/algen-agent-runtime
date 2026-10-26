"""Describe what a container would register, without building it or calling any component."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime import __version__
from algen_agent_runtime.analytics import (
    AnalyticalNodeKind,
    AnalyticalNodeRegistry,
    builtin_handlers,
)
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.context.artifacts import ContextArtifactService
from algen_agent_runtime.context.builder import ContextBuilderRegistry, DefaultContextBuilder
from algen_agent_runtime.context.skills import skill_load_tool
from algen_agent_runtime.context.working_state import working_state_tool
from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.mcp.contracts import MCPServerConfigBase
from algen_agent_runtime.memory.service import LearnedMemoryService
from algen_agent_runtime.memory.store import InMemoryLearnedMemoryStore
from algen_agent_runtime.methods import AnalyticalMethodRegistry
from algen_agent_runtime.model_services import ModelServiceRegistry
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.orchestration.container import (
    DEFAULT_PROVIDER_API_KEYS,
    SUPPORTED_PROVIDER_TYPES,
    base_tools,
    declared_plugin_names,
    load_declared_plugins,
)
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore, InMemoryMemoryStore
from algen_agent_runtime.planning.planners import PlannerRegistry
from algen_agent_runtime.plugins.registration import RuntimePluginContext
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.responses.composer import ResponseComposerRegistry
from algen_agent_runtime.retrieval.registry import RetrieverRegistry
from algen_agent_runtime.semantics import SemanticLayerRegistry
from algen_agent_runtime.tools.contracts import Idempotency, SideEffect, Tool, ToolContext
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.verification.verifiers import VerificationService

RUNTIME = "runtime"
"""Source of a component the Runtime itself provides."""
MANIFEST = "manifest"
"""Source of a component declared in the settings (agent.yaml)."""

INVENTORY_SCHEMA_VERSION = "1"


class _Description(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ComponentDescription(_Description):
    name: str
    source: str


class ToolDescription(_Description):
    name: str
    version: str
    description: str
    source: str
    side_effect: SideEffect
    idempotency: Idempotency
    required_permissions: tuple[str, ...]
    timeout_seconds: float
    max_attempts: int
    requires_approval: bool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    env: tuple[str, ...] = ()


class ProviderDescription(_Description):
    name: str
    type: str | None
    source: str
    env: tuple[str, ...] = ()


class MCPServerDescription(_Description):
    name: str
    transport: str
    prefix: str | None
    allowed_tools: tuple[str, ...]
    required: bool
    source: str
    env: tuple[str, ...] = ()


class PluginDescription(_Description):
    reference: str
    name: str
    version: str
    publisher: str
    capabilities: tuple[str, ...]
    env: tuple[str, ...]


class ComponentInventory(_Description):
    """Everything a container built from these settings would register, by kind.

    ``source`` is ``runtime`` for built-ins, ``manifest`` for components the settings declare,
    and the plugin reference for components a plugin registers. MCP tools are discovered at
    connect, so only their servers appear here.
    """

    schema_version: str = INVENTORY_SCHEMA_VERSION
    runtime_version: str = __version__
    plugins: tuple[PluginDescription, ...] = ()
    tools: tuple[ToolDescription, ...] = ()
    planners: tuple[ComponentDescription, ...] = ()
    response_composers: tuple[ComponentDescription, ...] = ()
    verifiers: tuple[ComponentDescription, ...] = ()
    context_builders: tuple[ComponentDescription, ...] = ()
    provider_types: tuple[str, ...] = ()
    providers: tuple[ProviderDescription, ...] = ()
    mcp_servers: tuple[MCPServerDescription, ...] = ()
    policies: tuple[ComponentDescription, ...] = ()
    analytical_nodes: tuple[ComponentDescription, ...] = Field(
        default=(),
        description="Analytical node kinds that have a handler; semantic_query and model_call "
        "have none unless a plugin supplies it.",
    )
    retrievers: tuple[ComponentDescription, ...] = ()
    semantic_layers: tuple[ComponentDescription, ...] = ()
    analytical_methods: tuple[ComponentDescription, ...] = ()
    model_services: tuple[ComponentDescription, ...] = ()
    environment: tuple[str, ...] = Field(
        default=(), description="Every environment variable name a deploy must supply."
    )


class _NamedPlaceholder:
    """Stands in for a manifest-declared context builder so plugin names are checked."""

    def __init__(self, name: str) -> None:
        self.name = name


async def _never_resolve(reference: str) -> str:
    raise ConfigurationError("describe_components never resolves secrets")


async def _never_called(context: ToolContext) -> Any:
    raise ConfigurationError("describe_components never calls a tool")


def _agent_runtime_tools() -> tuple[Tool, ...]:
    """The tools ``AgentRuntime`` registers itself for a container built by ``build_container``."""
    return (
        *LearnedMemoryService(InMemoryLearnedMemoryStore(), _never_called).tools(),
        working_state_tool(),
        skill_load_tool(_never_called),
        ContextArtifactService(InMemoryArtifactStore()).read_tool(),
    )


def env_references(value: Any) -> tuple[str, ...]:
    """The variable names of every ``env://`` reference inside a settings value."""
    found: set[str] = set()

    def walk(item: Any) -> None:
        if isinstance(item, BaseModel):
            walk(item.model_dump(mode="json"))
        elif isinstance(item, str):
            if item.startswith("env://"):
                found.add(item.removeprefix("env://"))
        elif isinstance(item, dict):
            for nested in item.values():
                walk(nested)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for nested in item:
                walk(nested)

    walk(value)
    return tuple(sorted(found))


def _components(
    names: Iterable[str], sources: dict[str, str], *, builtins: set[str]
) -> tuple[ComponentDescription, ...]:
    return tuple(
        ComponentDescription(
            name=name,
            source=sources.get(name) or (RUNTIME if name in builtins else MANIFEST),
        )
        for name in sorted(names)
    )


def _tool(tool: Tool, source: str, env: tuple[str, ...]) -> ToolDescription:
    definition = tool.definition
    return ToolDescription(
        name=definition.name,
        version=definition.version,
        description=definition.description,
        source=source,
        side_effect=definition.side_effect,
        idempotency=definition.idempotency,
        required_permissions=tuple(sorted(definition.required_permissions)),
        timeout_seconds=definition.timeout_seconds,
        max_attempts=definition.retry_policy.max_attempts,
        requires_approval=definition.requires_approval,
        input_schema=definition.input_schema,
        output_schema=definition.output_schema,
        env=env,
    )


def describe_components(settings: AppSettings) -> ComponentInventory:
    """Inventory every component a container built from ``settings`` would register.

    Declared plugins are imported and their ``register(context)`` runs, but no tool handler,
    planner, provider or other component is called, no secret is resolved and nothing
    connects to a network or a database.
    """
    tools = ToolRegistry()
    for tool in (*base_tools(settings, secrets=_never_resolve), *_agent_runtime_tools()):
        tools.register(tool)
    builtin_tools = {tool.definition.name for tool in tools.list()} - {
        declared.name for declared in settings.http_tools
    }
    planners = PlannerRegistry()
    memory = InMemoryMemoryStore()
    contexts = ContextBuilderRegistry(DefaultContextBuilder(memory))
    policies = CompositePolicyEngine()
    analytical_nodes = AnalyticalNodeRegistry()
    for kind, handler in builtin_handlers().items():
        analytical_nodes.register(AnalyticalNodeKind(kind), handler)
    retrievers = RetrieverRegistry()
    verifiers = VerificationService()
    composers = ResponseComposerRegistry()
    builtin = {
        "planners": set(planners.names()),
        "context_builders": set(contexts.names()),
        "verifiers": set(verifiers.names()),
        "response_composers": set(composers.names()),
        "policies": set(policies.list()),
        "analytical_nodes": {kind.value for kind in analytical_nodes.kinds()},
    }
    for name in settings.retrieval:
        contexts.register(cast(Any, _NamedPlaceholder(f"retrieval.{name}")))
        retrievers.register(name, cast(Any, _NamedPlaceholder(name)))
    semantics = SemanticLayerRegistry()
    methods = AnalyticalMethodRegistry()
    model_services = ModelServiceRegistry()
    mcp_servers: list[MCPServerConfigBase] = list(settings.mcp_servers)
    context = RuntimePluginContext(
        tools=tools,
        planners=planners,
        contexts=contexts,
        verifiers=verifiers,
        composers=composers,
        semantics=semantics,
        methods=methods,
        model_services=model_services,
        router=ModelRouter(),
        mcp_servers=mcp_servers,
        policies=policies,
        analytical_nodes=analytical_nodes,
        retrievers=retrievers,
        memory=memory,
        reserved_providers=frozenset(settings.providers),
        secrets=_never_resolve,
    )
    manifests = load_declared_plugins(settings, context)
    sources = context.sources.as_dict()

    plugins = tuple(
        PluginDescription(
            reference=reference,
            name=manifest.name,
            version=manifest.version,
            publisher=manifest.publisher,
            capabilities=tuple(sorted(manifest.capabilities)),
            env=tuple(sorted(manifest.env | context.sources.env.get(reference, set()))),
        )
        for reference, manifest in zip(declared_plugin_names(settings), manifests, strict=True)
    )
    plugin_env = {plugin.reference: plugin.env for plugin in plugins}
    http_tool_env = {declared.name: env_references(declared) for declared in settings.http_tools}
    tool_sources = sources.get("tools", {})

    described_tools = []
    for tool in sorted(tools.list(), key=lambda item: item.definition.name):
        name = tool.definition.name
        if name in tool_sources:
            described_tools.append(
                _tool(tool, tool_sources[name], plugin_env.get(tool_sources[name], ()))
            )
        elif name in builtin_tools:
            described_tools.append(_tool(tool, RUNTIME, ()))
        else:
            described_tools.append(_tool(tool, MANIFEST, http_tool_env.get(name, ())))

    providers = [
        ProviderDescription(
            name=name,
            type=config.type,
            source=MANIFEST,
            env=env_references(
                (config, config.api_key or DEFAULT_PROVIDER_API_KEYS.get(config.type))
            ),
        )
        for name, config in settings.providers.items()
    ]
    providers.extend(
        ProviderDescription(name=name, type=None, source=source, env=plugin_env.get(source, ()))
        for name, source in sources.get("providers", {}).items()
    )
    mcp_sources = sources.get("mcp_servers", {})
    environment = {
        *env_references(settings),
        *(variable for provider in providers for variable in provider.env),
        *(variable for plugin in plugins for variable in plugin.env),
    }
    return ComponentInventory(
        plugins=plugins,
        tools=tuple(described_tools),
        planners=_components(
            planners.names(), sources.get("planners", {}), builtins=builtin["planners"]
        ),
        response_composers=_components(
            composers.names(),
            sources.get("response_composers", {}),
            builtins=builtin["response_composers"],
        ),
        verifiers=_components(
            verifiers.names(), sources.get("verifiers", {}), builtins=builtin["verifiers"]
        ),
        context_builders=_components(
            contexts.names(),
            sources.get("context_builders", {}),
            builtins=builtin["context_builders"],
        ),
        provider_types=tuple(sorted(SUPPORTED_PROVIDER_TYPES)),
        providers=tuple(sorted(providers, key=lambda item: item.name)),
        mcp_servers=tuple(
            MCPServerDescription(
                name=server.name,
                transport=str(server.transport),
                prefix=server.prefix,
                allowed_tools=tuple(sorted(server.allowed_tools)),
                required=server.required,
                source=mcp_sources.get(server.name, MANIFEST),
                env=env_references(server),
            )
            for server in sorted(mcp_servers, key=lambda item: item.name)
        ),
        semantic_layers=_components(
            semantics.list(), sources.get("semantic_layers", {}), builtins=set()
        ),
        analytical_methods=_components(
            (
                f"{manifest.name}@{manifest.version}"
                for manifest in methods.discover(include_deprecated=True)
            ),
            sources.get("methods", {}),
            builtins=set(),
        ),
        model_services=_components(
            (f"{manifest.name}@{manifest.version}" for manifest in model_services.discover()),
            sources.get("model_services", {}),
            builtins=set(),
        ),
        policies=_components(
            policies.list(), sources.get("policies", {}), builtins=builtin["policies"]
        ),
        analytical_nodes=_components(
            (kind.value for kind in analytical_nodes.kinds()),
            sources.get("analytical_nodes", {}),
            builtins=builtin["analytical_nodes"],
        ),
        retrievers=_components(retrievers.list(), sources.get("retrievers", {}), builtins=set()),
        environment=tuple(sorted(environment)),
    )

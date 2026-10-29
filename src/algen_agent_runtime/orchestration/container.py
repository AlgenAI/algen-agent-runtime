from __future__ import annotations

import inspect
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import httpx

if TYPE_CHECKING:
    from algen_agent_runtime.mcp.client import MCPClientManager
    from algen_agent_runtime.mcp.contracts import MCPServerConfigBase

from algen_agent_runtime.analytics import (
    AnalyticalGraphEngine,
    AnalyticalNodeKind,
    AnalyticalNodeRegistry,
    InMemoryAnalyticalGraphStore,
    PostgresAnalyticalGraphStore,
    builtin_handlers,
)
from algen_agent_runtime.api.rate_limiting import (
    ApiRateLimiter,
    InMemoryApiRateLimiter,
    RouteLimitPolicy,
)
from algen_agent_runtime.approvals.service import (
    ApprovalService,
    InMemoryApprovalService,
    PostgresApprovalService,
)
from algen_agent_runtime.auth.revocation import (
    InMemoryTokenRevocationStore,
    PostgresTokenRevocationStore,
    TokenRevocationStore,
)
from algen_agent_runtime.cache import (
    CachePolicy,
    CacheService,
    InMemoryCacheStore,
    NullCacheStore,
    RedisCacheStore,
)
from algen_agent_runtime.cache.contracts import CacheStore
from algen_agent_runtime.config.registry import InMemoryAgentRegistry
from algen_agent_runtime.config.settings import AppSettings, EnvironmentSecretProvider
from algen_agent_runtime.context.artifacts import ContextArtifactService
from algen_agent_runtime.context.builder import (
    ContextBuilderRegistry,
    DefaultContextBuilder,
    RetrievalContextBuilder,
)
from algen_agent_runtime.conversations import ConversationPresentation, PresentationAudience
from algen_agent_runtime.conversations.feedback import (
    InMemoryConversationFeedbackStore,
    PostgresConversationFeedbackStore,
)
from algen_agent_runtime.conversations.followups import ModelFollowupSuggestionProvider
from algen_agent_runtime.conversations.service import (
    ConversationHandlerRegistry,
    ConversationService,
    RuntimeConversationHandler,
)
from algen_agent_runtime.conversations.stores import (
    InMemoryConversationEventBus,
    InMemoryConversationStore,
    PostgresConversationEventBus,
    PostgresConversationStore,
)
from algen_agent_runtime.distributed import InMemoryWorkQueue, PostgresWorkQueue, WorkQueue
from algen_agent_runtime.evaluation import EvaluationRunner
from algen_agent_runtime.events.bus import (
    InMemoryAuditLog,
    InMemoryEventBus,
    PostgresAuditLog,
    PostgresEventBus,
)
from algen_agent_runtime.exceptions.errors import ConfigurationError, ConflictError
from algen_agent_runtime.frameworks import FrameworkAdapter, FrameworkAdapterRegistry
from algen_agent_runtime.governance import QueryGovernanceEngine, QueryGovernancePolicy
from algen_agent_runtime.memory.store import InMemoryLearnedMemoryStore, PostgresLearnedMemoryStore
from algen_agent_runtime.methods import AnalyticalMethodRegistry
from algen_agent_runtime.model_services import ModelServiceRegistry
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.metering import UsageMeter
from algen_agent_runtime.models.providers.adapters import (
    AnthropicProvider,
    AzureOpenAIProvider,
    DeepSeekProvider,
    GeminiProvider,
    GroqProvider,
    HuggingFaceInferenceProvider,
    MistralProvider,
    OllamaProvider,
    OpenAIProvider,
    XAIProvider,
)
from algen_agent_runtime.models.providers.local_transformers import LocalTransformersProvider
from algen_agent_runtime.models.providers.openai_compatible import OpenAICompatibleProvider
from algen_agent_runtime.observability.traccia_adapter import (
    NoopObservabilityAdapter,
    ObservabilityAdapter,
    observability_adapter,
)
from algen_agent_runtime.observability.trace_levels import TraceLevelTracer
from algen_agent_runtime.orchestration.lifecycle import ContainerLifecycle, OwnedWorkflows
from algen_agent_runtime.orchestration.maintenance import MaintenanceService
from algen_agent_runtime.orchestration.readiness import ReadinessChecker, declare_dependencies
from algen_agent_runtime.orchestration.validation import (
    AvailableComponents,
    ComponentProblem,
    mcp_tool_prefix,
    unknown_agent_components,
    unknown_mcp_tools,
)
from algen_agent_runtime.persistence.memory import (
    InMemoryArtifactStore,
    InMemoryMemoryStore,
    InMemoryRunStore,
)
from algen_agent_runtime.persistence.postgres import (
    PostgresArtifactStore,
    PostgresDatabase,
    PostgresMemoryStore,
    PostgresRunStore,
    PostgresToolExecutionStore,
)
from algen_agent_runtime.persistence.redis import RedisMemoryStore, RedisRunStore
from algen_agent_runtime.persistence.retention import PostgresRetentionSweeper
from algen_agent_runtime.persistence.s3 import S3ArtifactStore
from algen_agent_runtime.persistence.tool_executions import InMemoryToolExecutionStore
from algen_agent_runtime.planning.planners import PlannerRegistry
from algen_agent_runtime.plugins.registration import RuntimePluginContext
from algen_agent_runtime.plugins.sdk import PluginLoader, PluginManifest
from algen_agent_runtime.policies.bundles import BundlePolicy, load_policy_bundles
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.responses.composer import ResponseComposerRegistry
from algen_agent_runtime.retrieval.ingestion import Chunker, MarkdownChunker, TextChunker
from algen_agent_runtime.retrieval.memory import (
    HashingEmbedder,
    InMemoryRetriever,
    ModelProviderEmbedder,
)
from algen_agent_runtime.retrieval.registry import RetrieverRegistry
from algen_agent_runtime.retrieval.vector_stores import (
    ChromaRetriever,
    FaissRetriever,
    PgVectorRetriever,
    QdrantRetriever,
)
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.semantics import SemanticLayerRegistry
from algen_agent_runtime.tools.builtin import http_tool, subprocess_tool
from algen_agent_runtime.tools.contracts import Tool, ToolExecutionStore
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.http_api import SecretResolver, http_api_tool
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.types.contracts import ModelCapabilities
from algen_agent_runtime.types.interfaces import (
    ArtifactStore,
    AuditLog,
    EventPublisher,
    MemoryStore,
    ModelProvider,
    RunStore,
    VectorStore,
)
from algen_agent_runtime.verification.verifiers import VerificationService
from algen_agent_runtime.workflows import (
    InMemoryWorkflowCheckpointStore,
    PostgresWorkflowCheckpointStore,
    WorkflowCheckpointStore,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Container:
    runtime: AgentRuntime
    agents: InMemoryAgentRegistry
    tools: ToolRegistry
    artifacts: ArtifactStore
    events: EventPublisher
    conversations: ConversationService
    workflow_checkpoints: WorkflowCheckpointStore = field(
        default_factory=InMemoryWorkflowCheckpointStore
    )
    cache: CacheService = field(default_factory=lambda: CacheService(NullCacheStore()))
    retrievers: RetrieverRegistry = field(default_factory=RetrieverRegistry)
    semantics: SemanticLayerRegistry = field(default_factory=SemanticLayerRegistry)
    observability: ObservabilityAdapter = field(default_factory=NoopObservabilityAdapter)
    frameworks: FrameworkAdapterRegistry = field(default_factory=FrameworkAdapterRegistry)
    analytical_graphs: AnalyticalGraphEngine | None = None
    analytical_methods: AnalyticalMethodRegistry = field(default_factory=AnalyticalMethodRegistry)
    model_services: ModelServiceRegistry = field(default_factory=ModelServiceRegistry)
    query_governance: QueryGovernanceEngine = field(
        default_factory=lambda: QueryGovernanceEngine(QueryGovernancePolicy())
    )
    evaluations: EvaluationRunner = field(default_factory=EvaluationRunner)
    work_queue: WorkQueue = field(default_factory=InMemoryWorkQueue)
    rate_limiter: ApiRateLimiter | None = None
    mcp: MCPClientManager | None = None
    degraded_dependencies: dict[str, str] = field(default_factory=dict)
    token_revocations: TokenRevocationStore | None = None
    """Store checked by verified API authentication when ``api.token_revocation`` is enabled;
    revoke through it from Python or through ``/v1/auth/revocations``."""
    oidc_transport: httpx.AsyncBaseTransport | None = None
    """Transport for OIDC discovery and JWKS fetches; ``None`` uses the SSRF-safe transport.
    Tests and hosts with their own egress layer may inject one."""
    resources: tuple[object, ...] = ()
    recover_incomplete_runs: bool = True
    recovery_limit: int = 1000
    shutdown_grace_seconds: float = 10.0
    maintenance: MaintenanceService | None = None
    run_maintenance_in_background: bool = False
    plugins: tuple[PluginManifest, ...] = ()
    component_sources: dict[str, dict[str, str]] = field(default_factory=dict)
    component_check: Literal["error", "warn"] = "error"
    # What /health/ready requires and reports; see orchestration/readiness.py.
    readiness: ReadinessChecker = field(default_factory=ReadinessChecker)
    # Start/drain phase and the service-owned workflow runs (attach_workflows).
    lifecycle: ContainerLifecycle = field(default_factory=ContainerLifecycle)
    recover_service_workflows: bool = True

    def attach_workflows(self, workflows: OwnedWorkflows) -> None:
        """Let the container recover, expire and drain the workflow runs a service owns."""
        self.lifecycle.workflows = workflows
        if self.maintenance is not None:
            self.maintenance.attach_workflows(workflows)

    async def readiness_report(self) -> tuple[bool, dict[str, Any]]:
        """``(ready, body)`` as ``/health/ready`` returns it."""
        return await self.readiness.report(self)

    async def astart(self) -> None:
        self.lifecycle.phase = "starting"
        self.observability.start()
        for resource in self.resources:
            start = getattr(resource, "start", None)
            if start is not None:
                result = start()
                if inspect.isawaitable(result):
                    await result
        if self.mcp is not None:
            for server_name in self.mcp.server_names:
                config = self.mcp.get_server(server_name)
                try:
                    tools = await self.mcp.discover_tools(server_name)
                    for tool in tools:
                        self.tools.register(tool)
                except Exception as exc:
                    from algen_agent_runtime.exceptions.errors import MCPNotConnectedError

                    # An OAuth server nobody has connected yet lists its tools on first connect.
                    if config.required and not isinstance(exc, MCPNotConnectedError):
                        raise ConfigurationError(
                            f"failed to discover required MCP server {server_name!r}: {exc}"
                        ) from exc
                    self.degraded_dependencies[f"mcp.{server_name}"] = str(exc)
                    continue
                _report_unknown_components(
                    unknown_mcp_tools(
                        self.agents.list(),
                        mcp_tool_prefix(config.name, config.prefix),
                        (tool.definition.name for tool in tools),
                    ),
                    self.component_check,
                )
        if self.recover_incomplete_runs:
            await self.runtime.recover(self.recovery_limit)
            await self.conversations.recover(self.recovery_limit)
            workflows = self.lifecycle.workflows
            if self.recover_service_workflows and workflows is not None:
                try:
                    await workflows.recover(self.recovery_limit)
                except Exception as exc:
                    # Serve the rest; readiness reports it and the next start tries again.
                    logger.exception("workflow recovery failed")
                    self.degraded_dependencies["workflow.recovery"] = type(exc).__name__
        if self.maintenance is not None and self.run_maintenance_in_background:
            self.maintenance.start()
        self.lifecycle.phase = "ready"

    async def resource_health(self) -> dict[str, bool]:
        health: dict[str, bool] = {}
        for resource in self.resources:
            name = type(resource).__name__
            probe = getattr(resource, "health", None) or getattr(resource, "ping", None)
            if probe is None:
                continue
            try:
                result = probe()
                if inspect.isawaitable(result):
                    result = await result
                health[name] = bool(result)
            except Exception:
                health[name] = False
        return health

    def close(self) -> None:
        self.observability.stop()

    async def aclose(self) -> None:
        self.lifecycle.phase = "draining"
        if self.maintenance is not None:
            await self.maintenance.stop()
        if self.lifecycle.workflows is not None:
            # Before the runtime drains, so workflow steps can still start agent runs.
            await self.lifecycle.workflows.aclose(self.shutdown_grace_seconds)
        await self.conversations.shutdown(self.shutdown_grace_seconds)
        await self.runtime.shutdown(self.shutdown_grace_seconds)
        for name in self.retrievers.list():
            close = getattr(self.retrievers.get(name), "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        for resource in reversed(self.resources):
            close = getattr(resource, "close", None) or getattr(resource, "aclose", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        self.close()
        self.lifecycle.phase = "stopped"


def _token_revocation_store(
    settings: AppSettings, database: PostgresDatabase | None
) -> TokenRevocationStore | None:
    revocation = settings.api.token_revocation
    if not revocation.enabled:
        return None
    if revocation.store == "postgres":
        return PostgresTokenRevocationStore(database)
    return InMemoryTokenRevocationStore()


class _ScopedEnvironmentSecretProvider:
    """Resolve per-container values before falling back to the process environment."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)
        self._fallback = EnvironmentSecretProvider()

    async def get(self, reference: str) -> str:
        if reference.startswith("env://"):
            name = reference.removeprefix("env://")
            if name in self._values:
                return self._values[name]
        return await self._fallback.get(reference)


def _resolve_environment_reference(
    value: str | None,
    name: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    if not value or not value.startswith("env://"):
        raise ValueError(f"{name} must be configured as an env:// reference")
    variable = value.removeprefix("env://")
    resolved = (environment or {}).get(variable) or os.getenv(variable)
    if not resolved:
        raise ValueError(f"required environment variable {variable!r} is not set")
    return resolved


DEFAULT_PROVIDER_API_KEYS: dict[str, str] = {
    "openai": "env://OPENAI_API_KEY",
    "azure_openai": "env://AZURE_OPENAI_API_KEY",
    "anthropic": "env://ANTHROPIC_API_KEY",
    "deepseek": "env://DEEPSEEK_API_KEY",
    "mistral": "env://MISTRAL_API_KEY",
    "huggingface_inference": "env://HF_TOKEN",
    "gemini": "env://GEMINI_API_KEY",
    "xai": "env://XAI_API_KEY",
    "groq": "env://GROQ_API_KEY",
}
"""The secret a provider type reads when its ``api_key`` is not configured."""

SUPPORTED_PROVIDER_TYPES: frozenset[str] = frozenset(
    {
        *DEFAULT_PROVIDER_API_KEYS,
        "ollama",
        "local_transformers",
        "openai_compatible",
        "mock",
    }
)


def base_tools(
    settings: AppSettings,
    *,
    secrets: SecretResolver,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[Tool, ...]:
    """The tools every container registers before plugins: core tools and manifest http_tools."""
    return (
        http_tool(
            allowed_hosts=settings.security.allowed_http_hosts,
            allow_private_networks=settings.security.allow_private_networks,
            client=http_client,
        ),
        subprocess_tool(enabled=settings.security.allow_subprocess_tools),
        *(
            http_api_tool(
                declared,
                allowed_hosts=settings.security.allowed_http_hosts,
                allow_private_networks=settings.security.allow_private_networks,
                secrets=secrets,
            )
            for declared in settings.http_tools
        ),
    )


def _report_unknown_components(
    problems: Sequence[ComponentProblem], mode: Literal["error", "warn"]
) -> None:
    if not problems:
        return
    message = "; ".join(problem.message for problem in problems)
    if mode == "warn":
        logger.warning("unregistered agent components: %s", message)
        return
    raise ConfigurationError(f"unregistered agent components: {message}")


def declared_plugin_names(settings: AppSettings) -> tuple[str, ...]:
    """Every agent's ``domain_plugins``, in declaration order, each named once."""
    return tuple(
        dict.fromkeys(name for definition in settings.agents for name in definition.domain_plugins)
    )


def load_declared_plugins(
    settings: AppSettings, context: RuntimePluginContext
) -> tuple[PluginManifest, ...]:
    names = declared_plugin_names(settings)
    loader = PluginLoader(settings.security.trusted_plugin_prefixes)
    loader.authorize(names)
    manifests = []
    for name in names:
        with context.registering(name):
            manifests.append(loader.load_one(name, context))
    return tuple(manifests)


def build_container(
    settings: AppSettings,
    *,
    environment: Mapping[str, str] | None = None,
    additional_tools: Sequence[Tool] = (),
    mcp_servers: Sequence[MCPServerConfigBase] = (),
    framework_adapters: Sequence[FrameworkAdapter] = (),
    usage_meter: UsageMeter | None = None,
    usage_meter_margin: float = 1.0,
) -> Container:
    """Build an isolated runtime container.

    ``environment`` supplies ephemeral, container-scoped values for ``env://``
    references. Values are never copied into settings or serialized manifests.

    ``usage_meter`` reserves and settles the token cost of every model call the
    router dispatches (see ``algen_agent_runtime.models.metering``). ``usage_meter_margin``
    scales the input tokens it reserves (``ModelRouter(reserve_input_margin=...)``), for
    example ``4 / 3`` to reserve as if a token were three characters; it must be at least 1.0.
    """
    environment = environment or {}
    secret_provider = _ScopedEnvironmentSecretProvider(environment)
    observability = observability_adapter(settings.telemetry, environment)
    agents = InMemoryAgentRegistry()
    for definition in settings.agents:
        agents.register(definition)
    tools = ToolRegistry()
    http_client = httpx.AsyncClient(follow_redirects=False)
    for tool in base_tools(settings, secrets=secret_provider.get, http_client=http_client):
        tools.register(tool)
    frameworks = FrameworkAdapterRegistry()
    for adapter in framework_adapters:
        frameworks.register(adapter)
    storage = settings.storage
    selected_backends = {
        storage.run_store,
        storage.memory_store,
        storage.learned_memory_store,
        storage.event_store,
        storage.audit_store,
        storage.approval_store,
        storage.artifact_store,
        storage.workflow_store,
        storage.tool_execution_store,
        storage.conversation_store,
        settings.analytical_execution.graph_store,
        settings.distributed_execution.queue_backend,
        storage.mcp_oauth_token_store,
    }
    if settings.api.token_revocation.enabled:
        selected_backends.add(settings.api.token_revocation.store)
    if storage.artifact_store == "s3":
        selected_backends.add("postgres")
    database = (
        PostgresDatabase(
            _resolve_environment_reference(storage.postgres_dsn, "postgres_dsn", environment),
            initialize_schema=storage.initialize_schema,
            min_pool_size=storage.postgres_min_pool_size,
            max_pool_size=storage.postgres_max_pool_size,
            schema=storage.postgres_schema,
        )
        if "postgres" in selected_backends
        else None
    )
    redis_client: object | None = None
    if "redis" in selected_backends:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:
            raise ImportError("install algen-agent-runtime[redis]") from exc
        redis_client = Redis.from_url(
            _resolve_environment_reference(storage.redis_url, "redis_url", environment)
        )
    cache_redis_client: object | None = None
    cache_store: CacheStore
    if settings.cache.backend == "redis":
        try:
            from redis.asyncio import Redis
        except ImportError as exc:
            raise ImportError("install algen-agent-runtime[redis]") from exc
        cache_url = _resolve_environment_reference(
            settings.cache.redis_url, "cache.redis_url", environment
        )
        storage_url = (
            _resolve_environment_reference(storage.redis_url, "redis_url", environment)
            if redis_client is not None
            else None
        )
        cache_redis_client = redis_client if storage_url == cache_url else Redis.from_url(cache_url)
    if settings.cache.backend == "redis":
        cache_store = RedisCacheStore(cache_redis_client, settings.cache.key_prefix)
    elif settings.cache.backend == "memory":
        cache_store = InMemoryCacheStore(settings.cache.max_entries)
    else:
        cache_store = NullCacheStore()
    cache_secret = (
        _resolve_environment_reference(
            settings.cache.key_secret,
            "cache.key_secret",
            environment,
        )
        if settings.cache.key_secret
        else None
    )
    cache = CacheService(
        cache_store,
        {
            name: CachePolicy.model_validate(policy.model_dump())
            for name, policy in settings.cache.policies.items()
        },
        key_secret=cache_secret,
        telemetry_trace_level=settings.telemetry.trace_level,
    )
    analytical_node_registry = AnalyticalNodeRegistry()
    for kind, handler in builtin_handlers().items():
        analytical_node_registry.register(AnalyticalNodeKind(kind), handler)
    analytical_graph_store = (
        PostgresAnalyticalGraphStore(database)
        if settings.analytical_execution.graph_store == "postgres" and database is not None
        else InMemoryAnalyticalGraphStore()
    )
    analytical_graphs = AnalyticalGraphEngine(
        analytical_node_registry,
        analytical_graph_store,
        cache=cache,
        maximum_concurrency=settings.analytical_execution.maximum_concurrency,
        maximum_node_timeout_seconds=(settings.analytical_execution.default_node_timeout_seconds),
        maximum_graph_timeout_seconds=(settings.analytical_execution.default_graph_timeout_seconds),
    )
    analytical_methods = AnalyticalMethodRegistry()
    model_services = ModelServiceRegistry()
    semantics = SemanticLayerRegistry()
    query_governance = QueryGovernanceEngine(
        QueryGovernancePolicy.model_validate(settings.query_governance.model_dump())
    )
    evaluations = EvaluationRunner()
    work_queue: WorkQueue = (
        PostgresWorkQueue(database)
        if settings.distributed_execution.queue_backend == "postgres" and database is not None
        else InMemoryWorkQueue()
    )
    runs: RunStore
    if storage.run_store == "postgres":
        runs = PostgresRunStore(database)
    elif storage.run_store == "redis":
        runs = RedisRunStore(redis_client)
    else:
        runs = InMemoryRunStore()
    memory: MemoryStore
    if storage.memory_store == "postgres":
        memory = PostgresMemoryStore(database, storage.memory_retention_seconds)
    elif storage.memory_store == "redis":
        memory = RedisMemoryStore(redis_client, retention_seconds=storage.memory_retention_seconds)
    else:
        memory = InMemoryMemoryStore()
    artifacts: ArtifactStore
    if storage.artifact_store == "postgres":
        artifacts = PostgresArtifactStore(database, settings.security.max_artifact_bytes)
    elif storage.artifact_store == "s3":
        artifacts = S3ArtifactStore(
            database,
            bucket=storage.artifact_s3_bucket or "",
            prefix=storage.artifact_s3_prefix,
            region=storage.artifact_s3_region,
            endpoint_url=storage.artifact_s3_endpoint_url,
            addressing_style=storage.artifact_s3_addressing_style,
            server_side_encryption=storage.artifact_s3_server_side_encryption,
            kms_key_id=storage.artifact_s3_kms_key_id,
            max_bytes=settings.security.max_artifact_bytes,
        )
    else:
        artifacts = InMemoryArtifactStore(settings.security.max_artifact_bytes)
    workflow_checkpoints: WorkflowCheckpointStore = (
        PostgresWorkflowCheckpointStore(database)
        if storage.workflow_store == "postgres"
        else InMemoryWorkflowCheckpointStore()
    )
    events: EventPublisher = (
        PostgresEventBus(database) if storage.event_store == "postgres" else InMemoryEventBus()
    )
    audits: AuditLog = (
        PostgresAuditLog(database) if storage.audit_store == "postgres" else InMemoryAuditLog()
    )
    policies = CompositePolicyEngine(
        guardrail_scope=observability.guardrail_scope,
        error_handling=settings.policies.error_handling(),
        audits=audits,
    )
    for bundle in load_policy_bundles(settings.policies, environment):
        policies.register(BundlePolicy(bundle), mandatory=True)
    planners = PlannerRegistry()
    contexts = ContextBuilderRegistry(DefaultContextBuilder(memory))
    verifiers = VerificationService()
    composers = ResponseComposerRegistry()
    approvals: ApprovalService = (
        PostgresApprovalService(database)
        if storage.approval_store == "postgres"
        else InMemoryApprovalService()
    )
    executions: ToolExecutionStore = (
        PostgresToolExecutionStore(database)
        if storage.tool_execution_store == "postgres"
        else InMemoryToolExecutionStore()
    )
    router = ModelRouter(
        cache=cache, usage_meter=usage_meter, reserve_input_margin=usage_meter_margin
    )
    for name, provider_config in settings.providers.items():
        kwargs: dict[str, Any] = {
            "default_model": provider_config.default_model,
            "stream_usage": provider_config.stream_usage,
        }
        provider: ModelProvider
        if provider_config.type == "openai":
            provider = OpenAIProvider(
                provider_config.api_key or DEFAULT_PROVIDER_API_KEYS["openai"],
                secret_provider=secret_provider,
                **kwargs,
            )
        elif provider_config.type == "azure_openai":
            provider = AzureOpenAIProvider(
                provider_config.base_url or "",
                provider_config.api_key or DEFAULT_PROVIDER_API_KEYS["azure_openai"],
                provider_config.default_model,
                provider_config.api_version or "2024-10-21",
                secret_provider=secret_provider,
                stream_usage=provider_config.stream_usage,
            )
        elif provider_config.type == "anthropic":
            provider = AnthropicProvider(
                provider_config.api_key or DEFAULT_PROVIDER_API_KEYS["anthropic"],
                secret_provider=secret_provider,
                prompt_caching=provider_config.prompt_caching,
                **kwargs,
            )
        elif provider_config.type == "deepseek":
            provider = DeepSeekProvider(
                provider_config.api_key or DEFAULT_PROVIDER_API_KEYS["deepseek"],
                secret_provider=secret_provider,
                **kwargs,
            )
        elif provider_config.type == "mistral":
            provider = MistralProvider(
                provider_config.api_key or DEFAULT_PROVIDER_API_KEYS["mistral"],
                base_url=provider_config.base_url or "https://api.mistral.ai/v1",
                capabilities=(
                    provider_config.capabilities
                    if "capabilities" in provider_config.model_fields_set
                    else None
                ),
                secret_provider=secret_provider,
                **kwargs,
            )
        elif provider_config.type == "ollama":
            provider = OllamaProvider(
                base_url=provider_config.base_url or "http://127.0.0.1:11434/v1",
                **kwargs,
            )
        elif provider_config.type in {"gemini", "xai", "groq"}:
            hosted = {"gemini": GeminiProvider, "xai": XAIProvider, "groq": GroqProvider}
            provider_class = hosted[provider_config.type]
            hosted_kwargs: dict[str, Any] = {}
            if provider_config.base_url:
                hosted_kwargs["base_url"] = provider_config.base_url
            provider = provider_class(
                provider_config.api_key or DEFAULT_PROVIDER_API_KEYS[provider_config.type],
                capabilities=(
                    provider_config.capabilities
                    if "capabilities" in provider_config.model_fields_set
                    else None
                ),
                secret_provider=secret_provider,
                **hosted_kwargs,
                **kwargs,
            )
        elif provider_config.type == "huggingface_inference":
            provider = HuggingFaceInferenceProvider(
                provider_config.api_key or DEFAULT_PROVIDER_API_KEYS["huggingface_inference"],
                secret_provider=secret_provider,
                **kwargs,
            )
        elif provider_config.type == "local_transformers":
            provider = LocalTransformersProvider(provider_config.default_model)
        elif provider_config.type == "openai_compatible":
            provider = OpenAICompatibleProvider(
                name,
                provider_config.base_url or "http://127.0.0.1:8001/v1",
                provider_config.api_key,
                provider_config.default_model,
                provider_config.capabilities,
                secret_provider=secret_provider,
                stream_usage=provider_config.stream_usage,
            )
        elif provider_config.type == "mock":
            from algen_agent_runtime.models.providers.mock import MockModelProvider

            provider = MockModelProvider()
        else:
            raise ValueError(f"unsupported provider type {provider_config.type!r}")

        # WP-02: compute a capability override when the operator explicitly configured
        # the capabilities field.  Configuration may narrow True→False; it must never
        # widen False→True.  An omitted capabilities field preserves adapter discovery.
        capability_override: ModelCapabilities | None = None
        if "capabilities" in provider_config.model_fields_set:
            capability_override = provider_config.capabilities

        router.register_provider(
            provider,
            provider_config.requests_per_minute,
            provider_config.cost_per_1k_input,
            provider_config.cost_per_1k_output,
            is_local=provider_config.type in {"ollama", "local_transformers"},
            registration_id=name,  # WP-01: YAML key is the stable routing identity
            capability_override=capability_override,  # WP-02: None → adapter discovery
        )
    retrievers = RetrieverRegistry()
    for name, retrieval_config in settings.retrieval.items():
        retriever: VectorStore
        embedder = (
            ModelProviderEmbedder(
                router.provider(retrieval_config.embedding_provider),
                retrieval_config.embedding_model,
            )
            if retrieval_config.embedding_provider
            else HashingEmbedder(retrieval_config.embedding_dimensions)
        )
        chunker: Chunker = (
            MarkdownChunker(retrieval_config.chunk_size, retrieval_config.chunk_overlap)
            if retrieval_config.chunker == "markdown"
            else TextChunker(retrieval_config.chunk_size, retrieval_config.chunk_overlap)
        )
        if retrieval_config.type == "memory":
            retriever = InMemoryRetriever(
                source_documents=retrieval_config.documents,
                embedder=embedder,
                chunker=chunker,
            )
        elif retrieval_config.type == "pgvector":
            connection_url = retrieval_config.connection_url or ""
            if connection_url.startswith("env://"):
                variable = connection_url.removeprefix("env://")
                connection_url = environment.get(variable) or os.getenv(variable) or ""
                if not connection_url:
                    raise ValueError(f"required environment variable {variable!r} is not set")
            retriever = PgVectorRetriever(
                connection_url,
                embedder,
                dimensions=retrieval_config.embedding_dimensions,
                table=retrieval_config.table_name,
                chunker=chunker,
                initialize_schema=retrieval_config.initialize_schema,
            )
        elif retrieval_config.type == "chroma":
            retriever = ChromaRetriever(
                embedder,
                collection_name=retrieval_config.collection_name,
                path=retrieval_config.persistence_path,
                chunker=chunker,
            )
        elif retrieval_config.type == "faiss":
            retriever = FaissRetriever(
                embedder,
                dimensions=retrieval_config.embedding_dimensions,
                path=retrieval_config.persistence_path,
                chunker=chunker,
            )
        else:
            api_key = None
            if retrieval_config.api_key:
                variable = retrieval_config.api_key.removeprefix("env://")
                api_key = environment.get(variable) or os.getenv(variable)
            connection_url = retrieval_config.connection_url or "http://localhost:6333"
            if connection_url.startswith("env://"):
                variable = connection_url.removeprefix("env://")
                connection_url = environment.get(variable) or os.getenv(variable) or ""
                if not connection_url:
                    raise ValueError(f"required environment variable {variable!r} is not set")
            retriever = QdrantRetriever(
                embedder,
                dimensions=retrieval_config.embedding_dimensions,
                collection_name=retrieval_config.collection_name,
                url=connection_url,
                api_key=api_key,
                chunker=chunker,
            )
        if retrieval_config.documents and retrieval_config.type != "memory":
            raise ValueError(
                f"retrieval {name!r}: persistent backends must be populated explicitly via ingest()"
            )
        retrievers.register(name, retriever)
        contexts.register(
            RetrievalContextBuilder(
                f"retrieval.{name}",
                memory,
                retriever,
                mode=retrieval_config.mode,
                limit=retrieval_config.limit,
                minimum_score=retrieval_config.minimum_score,
                keyword_weight=retrieval_config.keyword_weight,
                vector_weight=retrieval_config.vector_weight,
                filters=retrieval_config.filters,
                max_query_chars=retrieval_config.max_query_chars,
                max_retrieval_tokens=retrieval_config.max_retrieval_tokens,
                max_context_tokens=retrieval_config.max_context_tokens,
                cache=cache,
            )
        )
    combined_mcp_servers: list[MCPServerConfigBase] = list(settings.mcp_servers)
    plugin_context = RuntimePluginContext(
        tools=tools,
        planners=planners,
        contexts=contexts,
        verifiers=verifiers,
        composers=composers,
        semantics=semantics,
        methods=analytical_methods,
        model_services=model_services,
        router=router,
        mcp_servers=combined_mcp_servers,
        policies=policies,
        analytical_nodes=analytical_node_registry,
        retrievers=retrievers,
        memory=memory,
        cache=cache,
        secrets=secret_provider.get,
    )
    plugin_manifests = load_declared_plugins(settings, plugin_context)
    for tool in additional_tools:
        tools.register(tool)
    existing_mcp_names = {s.name for s in combined_mcp_servers}
    for s in mcp_servers:
        if s.name in existing_mcp_names:
            raise ConflictError(f"MCP server {s.name!r} already declared in settings")
        combined_mcp_servers.append(s)
        existing_mcp_names.add(s.name)

    mcp_manager: MCPClientManager | None = None
    if combined_mcp_servers:
        from algen_agent_runtime.mcp.client import MCPClientManager
        from algen_agent_runtime.mcp.oauth import build_mcp_oauth_manager

        mcp_manager = MCPClientManager(
            combined_mcp_servers,
            environment=environment,
            security=settings.security,
            oauth=build_mcp_oauth_manager(
                combined_mcp_servers,
                storage,
                database=database,
                environment=environment,
                security=settings.security,
            ),
        )
    executor = ToolExecutor(
        tools,
        policies,
        executions,
        cache=cache,
        # The adapter's tracer, filtered by trace level like the Runtime's own spans.
        tracer=TraceLevelTracer(
            observability.tracer("algen_agent_runtime.tools"), settings.telemetry.trace_level
        ),
    )
    runtime = AgentRuntime(
        agents=agents,
        router=router,
        tools=tools,
        tool_executor=executor,
        planners=planners,
        contexts=contexts,
        policies=policies,
        verifiers=verifiers,
        composers=composers,
        runs=runs,
        memory=memory,
        events=events,
        approvals=approvals,
        audits=audits,
        observability=observability,
        cache=cache,
        context_artifacts=ContextArtifactService(artifacts),
        learned_memory_store=(
            PostgresLearnedMemoryStore(database)
            if storage.learned_memory_store == "postgres"
            else InMemoryLearnedMemoryStore()
        ),
        telemetry_include_content=settings.telemetry.include_content,
        telemetry_max_content_chars=settings.telemetry.max_content_chars,
        telemetry_trace_level=settings.telemetry.trace_level,
    )
    _report_unknown_components(
        unknown_agent_components(
            settings.agents,
            AvailableComponents(
                tools=frozenset(tool.definition.name for tool in tools.list()),
                planners=frozenset(planners.names()),
                response_composers=frozenset(composers.names()),
                verifiers=frozenset(verifiers.names()),
                context_builders=frozenset(contexts.names()),
                providers=frozenset(router.registration_ids()),
                policies=frozenset(policies.list()),
                deferred_tool_prefixes=tuple(
                    mcp_tool_prefix(server.name, server.prefix) for server in combined_mcp_servers
                ),
            ),
        ),
        settings.runtime.component_check,
    )
    conversation_store = (
        PostgresConversationStore(database)
        if storage.conversation_store == "postgres"
        else InMemoryConversationStore()
    )
    conversation_events = (
        PostgresConversationEventBus(database)
        if storage.conversation_store == "postgres"
        else InMemoryConversationEventBus()
    )
    conversation_feedback = (
        PostgresConversationFeedbackStore(database)
        if storage.conversation_store == "postgres"
        else InMemoryConversationFeedbackStore()
    )
    followup_provider = (
        ModelFollowupSuggestionProvider(
            runtime,
            agent=settings.conversation_followups.agent,
            max_suggestions=settings.conversation_followups.max_suggestions,
            history_messages=settings.conversation_followups.history_messages,
            maximum_length=settings.conversation_followups.maximum_length,
        )
        if settings.conversation_followups.enabled
        else None
    )
    presentation = ConversationPresentation(
        progress_audience=PresentationAudience(
            settings.conversation_presentation.progress_audience
        ),
        error_audience=PresentationAudience(settings.conversation_presentation.error_audience),
        show_technical_details=settings.conversation_presentation.show_technical_details,
        technical_details_expanded=(settings.conversation_presentation.technical_details_expanded),
    )
    conversation_handlers = ConversationHandlerRegistry()
    conversation_handlers.register(
        RuntimeConversationHandler(
            runtime,
            presentation,
            stream=settings.conversation_streaming.enabled,
            delta_interval_seconds=settings.conversation_streaming.delta_interval_ms / 1000,
            delta_max_chars=settings.conversation_streaming.delta_max_chars,
        )
    )
    conversations = ConversationService(
        conversation_store,
        conversation_events,
        conversation_handlers,
        audits,
        observability=observability,
        telemetry_include_content=settings.telemetry.include_conversation_content,
        telemetry_max_content_chars=settings.telemetry.max_content_chars,
        presentation=presentation,
        feedback_store=conversation_feedback,
        followup_provider=followup_provider,
    )
    rate_limiter: ApiRateLimiter | None = None
    if settings.api.rate_limiting.enabled:
        route_policies = {
            name: RouteLimitPolicy(
                rate_per_minute=policy.rate_per_minute,
                burst=policy.burst,
                max_concurrent=policy.max_concurrent,
            )
            for name, policy in settings.api.rate_limiting.route_overrides.items()
        }
        rate_limiter = InMemoryApiRateLimiter(
            default_rate_per_minute=settings.api.rate_limiting.default_rate_per_minute,
            default_burst=settings.api.rate_limiting.default_burst,
            max_concurrent_run_requests_per_tenant=settings.api.rate_limiting.max_concurrent_run_requests_per_tenant,
            route_policies=route_policies,
            fail_closed=settings.api.rate_limiting.fail_closed,
        )
    lifecycle_resources = list(
        dict.fromkeys(
            resource
            for resource in (
                database,
                redis_client,
                cache_redis_client,
                artifacts if storage.artifact_store == "s3" else None,
                http_client,
                mcp_manager,
                *plugin_context.resources,
            )
            if resource is not None
        )
    )
    for adapter in framework_adapters:
        if all(adapter is not resource for resource in lifecycle_resources):
            lifecycle_resources.append(adapter)
    return Container(
        runtime=runtime,
        agents=agents,
        tools=tools,
        artifacts=artifacts,
        events=events,
        conversations=conversations,
        workflow_checkpoints=workflow_checkpoints,
        cache=cache,
        retrievers=retrievers,
        semantics=semantics,
        frameworks=frameworks,
        observability=observability,
        analytical_graphs=analytical_graphs,
        analytical_methods=analytical_methods,
        model_services=model_services,
        query_governance=query_governance,
        evaluations=evaluations,
        work_queue=work_queue,
        rate_limiter=rate_limiter,
        resources=tuple(lifecycle_resources),
        mcp=mcp_manager,
        token_revocations=_token_revocation_store(settings, database),
        recover_incomplete_runs=settings.runtime.recover_incomplete_runs,
        recovery_limit=settings.runtime.recovery_limit,
        shutdown_grace_seconds=settings.runtime.shutdown_grace_seconds,
        maintenance=MaintenanceService(
            runtime,
            artifacts,
            settings.maintenance,
            PostgresRetentionSweeper(database) if database is not None else None,
            conversations=conversations,
        ),
        run_maintenance_in_background=settings.maintenance.enabled,
        plugins=plugin_manifests,
        component_sources=plugin_context.sources.as_dict(),
        component_check=settings.runtime.component_check,
        readiness=declare_dependencies(
            settings,
            database=database,
            redis_client=redis_client,
            cache_redis_client=cache_redis_client,
            artifacts=artifacts,
            observability=observability,
        ),
        recover_service_workflows=settings.runtime.recover_service_workflows,
    )

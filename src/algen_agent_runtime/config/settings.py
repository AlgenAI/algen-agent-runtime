from __future__ import annotations

import ipaddress
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from algen_agent_runtime.cache.contracts import CacheScope
from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.retrieval.contracts import RetrievalMode, SourceDocument
from algen_agent_runtime.types.contracts import AgentDefinition, ModelCapabilities


class StrictSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderSettings(StrictSettings):
    type: str
    base_url: str | None = None
    api_key: str | None = Field(default=None, description="Secret reference, for example env://KEY")
    api_version: str | None = None
    organization: str | None = None
    default_model: str
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    timeout_seconds: float = Field(default=60, gt=0)
    max_concurrency: int = Field(default=20, ge=1)
    requests_per_minute: int = Field(default=600, ge=1)
    cost_per_1k_input: float = Field(default=0, ge=0)
    cost_per_1k_output: float = Field(default=0, ge=0)
    extensions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_literal_secret(self) -> ProviderSettings:
        if self.api_key and not self.api_key.startswith(("env://", "secret://")):
            raise ValueError("api_key must be a secret reference (env:// or secret://)")
        return self


class StorageSettings(StrictSettings):
    run_store: Literal["memory", "postgres", "redis"] = "memory"
    memory_store: Literal["memory", "postgres", "redis"] = "memory"
    event_store: Literal["memory", "postgres"] = "memory"
    audit_store: Literal["memory", "postgres"] = "memory"
    approval_store: Literal["memory", "postgres"] = "memory"
    artifact_store: Literal["memory", "postgres"] = "memory"
    tool_execution_store: Literal["memory", "postgres"] = "memory"
    conversation_store: Literal["memory", "postgres"] = "memory"
    postgres_dsn: str | None = None
    redis_url: str | None = None
    initialize_schema: bool = True
    postgres_min_pool_size: int = Field(default=1, ge=1)
    postgres_max_pool_size: int = Field(default=10, ge=1)
    memory_retention_seconds: int = Field(default=86_400, ge=0)

    @model_validator(mode="after")
    def validate_backends(self) -> StorageSettings:
        selected = {
            self.run_store,
            self.memory_store,
            self.event_store,
            self.audit_store,
            self.approval_store,
            self.artifact_store,
            self.tool_execution_store,
            self.conversation_store,
        }
        if "postgres" in selected and not self.postgres_dsn:
            raise ValueError("postgres-backed stores require postgres_dsn")
        if "redis" in selected and not self.redis_url:
            raise ValueError("redis-backed stores require redis_url")
        for field_name in ("postgres_dsn", "redis_url"):
            value = getattr(self, field_name)
            if value and not value.startswith("env://"):
                raise ValueError(f"{field_name} must be an env:// secret reference")
        if self.postgres_min_pool_size > self.postgres_max_pool_size:
            raise ValueError("postgres_min_pool_size cannot exceed postgres_max_pool_size")
        return self


class CachePolicySettings(StrictSettings):
    enabled: bool = False
    scope: CacheScope = CacheScope.TENANT
    ttl_seconds: int = Field(default=300, ge=1, le=2_592_000)
    maximum_value_bytes: int = Field(default=1_048_576, ge=1, le=100_000_000)


class CacheSettings(StrictSettings):
    backend: Literal["none", "memory", "redis"] = "none"
    redis_url: str | None = Field(default=None, description="env:// Redis connection URL")
    key_secret: str | None = Field(default=None, description="env:// HMAC key for cache keys")
    key_prefix: str = Field(default="algen-agent-runtime:cache", min_length=1, max_length=128)
    max_entries: int = Field(default=10_000, ge=1, le=10_000_000)
    policies: dict[str, CachePolicySettings] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_cache(self) -> CacheSettings:
        if self.backend == "redis" and not self.redis_url:
            raise ValueError("Redis caching requires redis_url")
        for field_name in ("redis_url", "key_secret"):
            value = getattr(self, field_name)
            if value and not value.startswith("env://"):
                raise ValueError(f"cache {field_name} must be an env:// secret reference")
        return self


class RetrievalSettings(StrictSettings):
    type: Literal["memory", "pgvector", "chroma", "faiss", "qdrant"] = "memory"
    mode: RetrievalMode = RetrievalMode.HYBRID
    limit: int = Field(default=6, ge=1, le=100)
    minimum_score: float = Field(default=0.05, ge=0, le=1)
    keyword_weight: float = Field(default=0.4, ge=0, le=1)
    vector_weight: float = Field(default=0.6, ge=0, le=1)
    filters: dict[str, Any] = Field(default_factory=dict)
    max_query_chars: int = Field(default=4_096, ge=128, le=100_000)
    max_retrieval_tokens: int = Field(default=4_000, ge=128)
    max_context_tokens: int = Field(default=16_000, ge=256)
    chunk_size: int = Field(default=1_200, ge=64)
    chunk_overlap: int = Field(default=200, ge=0)
    embedding_provider: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int = Field(default=1536, ge=8)
    connection_url: str | None = None
    api_key: str | None = None
    collection_name: str = "algen_agent_runtime_documents"
    table_name: str = "algen_agent_runtime_documents"
    persistence_path: str | None = None
    initialize_schema: bool = True
    documents: tuple[SourceDocument, ...] = ()

    @model_validator(mode="after")
    def validate_retrieval(self) -> RetrievalSettings:
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        if self.mode == RetrievalMode.HYBRID and self.keyword_weight + self.vector_weight == 0:
            raise ValueError("hybrid retrieval requires a non-zero retrieval weight")
        if self.type == "pgvector" and not self.connection_url:
            raise ValueError("pgvector retrieval requires connection_url")
        if self.type in {"chroma", "faiss", "qdrant"} and self.mode != RetrievalMode.VECTOR:
            raise ValueError(f"{self.type} currently supports vector retrieval mode only")
        if self.api_key and not self.api_key.startswith("env://"):
            raise ValueError("retrieval api_key must be an env:// reference")
        return self


class TracciaSettings(StrictSettings):
    enabled: bool = False
    api_key: str | None = Field(
        default=None, description="Optional env:// reference; the SDK can also read TRACCIA_API_KEY"
    )
    endpoint: str | None = None
    metrics_endpoint: str | None = None
    environment: str = "development"
    project_id: str | None = None
    sample_rate: float = Field(default=1.0, ge=0, le=1)
    service_role: Literal["orchestrator"] = "orchestrator"
    enable_patching: bool = False
    enable_token_counting: bool = True
    enable_costs: bool = True
    enable_metrics: bool = True
    redact_pii: bool = True
    max_spans_per_second: float | None = Field(default=None, gt=0)
    flush_timeout_seconds: float = Field(default=5.0, gt=0)
    governance_enabled: bool = False
    governance_fail_open: bool = False
    governance_agent_id: str | None = None

    @model_validator(mode="after")
    def validate_api_key_reference(self) -> TracciaSettings:
        if self.api_key and not self.api_key.startswith("env://"):
            raise ValueError("Traccia api_key must be an env:// reference")
        if self.governance_enabled and not self.enabled:
            raise ValueError("Traccia governance requires Traccia telemetry to be enabled")
        return self


class TelemetrySettings(StrictSettings):
    enabled: bool = True
    service_name: str = "algen-agent-runtime"
    trace_level: Literal["minimal", "standard", "detailed"] = "detailed"
    otlp_endpoint: str | None = None
    include_content: bool = False
    include_conversation_content: bool = False
    max_content_chars: int = Field(default=16_384, ge=256, le=1_048_576)
    traccia: TracciaSettings = Field(default_factory=TracciaSettings)


class SecuritySettings(StrictSettings):
    max_request_bytes: int = Field(default=1_048_576, ge=1024)
    max_artifact_bytes: int = Field(default=10_485_760, ge=1024)
    allowed_http_hosts: tuple[str, ...] = ()
    allow_private_networks: bool = False
    allow_subprocess_tools: bool = False
    trusted_plugin_prefixes: tuple[str, ...] = ()


class ApiSettings(StrictSettings):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    auth_mode: Literal["development_headers", "jwt"] = "development_headers"
    allow_insecure_development_auth: bool = False
    jwt_key: str | None = Field(default=None, description="env:// signing or verification key")
    jwt_algorithms: tuple[str, ...] = ("RS256",)
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    tenant_claim: str = "tenant_id"
    scopes_claim: str = "scope"
    cors_allowed_origins: tuple[str, ...] = ()
    cors_allow_credentials: bool = False
    cors_allowed_headers: tuple[str, ...] = (
        "Authorization",
        "Content-Type",
        "Last-Event-ID",
        "X-Tenant-ID",
        "X-User-ID",
        "X-Scopes",
    )

    @model_validator(mode="after")
    def validate_authentication(self) -> ApiSettings:
        try:
            loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            loopback = self.host.lower() == "localhost"
        if (
            self.auth_mode == "development_headers"
            and not loopback
            and not self.allow_insecure_development_auth
        ):
            raise ValueError(
                "development_headers authentication may bind only to a loopback host; "
                "set allow_insecure_development_auth=true only for an explicitly trusted "
                "development network"
            )
        if self.auth_mode == "jwt" and not self.jwt_key:
            raise ValueError("jwt authentication requires jwt_key")
        if self.jwt_key and not self.jwt_key.startswith("env://"):
            raise ValueError("jwt_key must be an env:// secret reference")
        return self


class RuntimeSettings(StrictSettings):
    recover_incomplete_runs: bool = True
    recovery_limit: int = Field(default=1000, ge=1, le=100_000)
    shutdown_grace_seconds: float = Field(default=10.0, ge=0, le=300)


class ConversationPresentationSettings(StrictSettings):
    progress_audience: Literal["business", "developer"] = "business"
    error_audience: Literal["business", "developer"] = "business"
    show_technical_details: bool = False
    technical_details_expanded: bool = False


class ConversationFollowupSettings(StrictSettings):
    enabled: bool = False
    agent: str = "conversation-followups"
    max_suggestions: int = Field(default=3, ge=1, le=10)
    history_messages: int = Field(default=8, ge=0, le=50)
    maximum_length: int = Field(default=240, ge=20, le=500)


class AnalyticalExecutionSettings(StrictSettings):
    graph_store: Literal["memory", "postgres"] = "memory"
    maximum_concurrency: int = Field(default=8, ge=1, le=128)
    default_node_timeout_seconds: float = Field(
        default=30,
        gt=0,
        le=3600,
        description="Deployment upper bound for a node timeout",
    )
    default_graph_timeout_seconds: float = Field(
        default=300,
        gt=0,
        le=86_400,
        description="Deployment upper bound for a graph timeout",
    )


class QueryGovernanceSettings(StrictSettings):
    allowed_purposes: tuple[str, ...] = ()
    required_authorization_tags: tuple[str, ...] = ()
    denied_columns: tuple[str, ...] = ()
    allowed_metrics: tuple[str, ...] = ()
    require_certified_sources: bool = True
    maximum_rows: int = Field(default=10_000, ge=1)
    maximum_bytes_scanned: int = Field(default=1_000_000_000, ge=1)
    maximum_compute_seconds: float = Field(default=30, gt=0)
    maximum_concurrency: int = Field(default=4, ge=1, le=128)
    allow_sql_visibility: bool = False
    allow_download: bool = False
    allow_artifact_export: bool = False
    audit_retention_days: int = Field(default=365, ge=1)


class DistributedExecutionSettings(StrictSettings):
    enabled: bool = False
    queue_backend: Literal["memory", "postgres"] = "memory"
    lease_seconds: int = Field(default=30, ge=5, le=3600)
    worker_concurrency: int = Field(default=4, ge=1, le=128)


class AppSettings(StrictSettings):
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)
    agents: tuple[AgentDefinition, ...] = ()
    storage: StorageSettings = Field(default_factory=StorageSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    retrieval: dict[str, RetrievalSettings] = Field(default_factory=dict)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    conversation_presentation: ConversationPresentationSettings = Field(
        default_factory=ConversationPresentationSettings
    )
    conversation_followups: ConversationFollowupSettings = Field(
        default_factory=ConversationFollowupSettings
    )
    analytical_execution: AnalyticalExecutionSettings = Field(
        default_factory=AnalyticalExecutionSettings
    )
    query_governance: QueryGovernanceSettings = Field(default_factory=QueryGovernanceSettings)
    distributed_execution: DistributedExecutionSettings = Field(
        default_factory=DistributedExecutionSettings
    )
    feature_flags: dict[str, bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_analytical_backends(self) -> AppSettings:
        needs_postgres = (
            self.analytical_execution.graph_store == "postgres"
            or self.distributed_execution.queue_backend == "postgres"
        )
        if needs_postgres and not self.storage.postgres_dsn:
            raise ValueError("PostgreSQL analytical execution requires storage.postgres_dsn")
        if (
            self.distributed_execution.enabled
            and self.distributed_execution.queue_backend == "memory"
        ):
            raise ValueError("distributed execution requires a durable postgres queue_backend")
        if self.conversation_followups.enabled and self.conversation_followups.agent not in {
            agent.name for agent in self.agents
        }:
            raise ValueError(
                "conversation follow-up generation requires its configured agent to be registered"
            )
        return self


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _parse_env_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _environment(prefix: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        parts = key[len(prefix) :].lower().split("__")
        cursor = root
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = _parse_env_value(value)
    return root


def load_settings(
    files: tuple[str | Path, ...] = (),
    deployment_overrides: dict[str, Any] | None = None,
) -> AppSettings:
    data: dict[str, Any] = {}
    try:
        for file in files:
            loaded = yaml.safe_load(Path(file).read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ConfigurationError(f"configuration root in {file} must be a mapping")
            data = _merge(data, loaded)
        data = _merge(data, _environment("ALGEN_AGENT_RUNTIME__"))
        data = _merge(data, deployment_overrides or {})
        return AppSettings.model_validate(data)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise ConfigurationError(f"invalid configuration: {exc}") from exc


class EnvironmentSecretProvider:
    async def get(self, reference: str) -> str:
        if not reference.startswith("env://"):
            raise ConfigurationError("environment secret provider only supports env:// references")
        name = reference.removeprefix("env://")
        value = os.getenv(name)
        if value is None:
            raise ConfigurationError(f"required secret environment variable {name!r} is not set")
        return SecretStr(value).get_secret_value()

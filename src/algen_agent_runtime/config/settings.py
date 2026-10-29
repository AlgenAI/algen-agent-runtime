from __future__ import annotations

import ipaddress
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from algen_agent_runtime.cache.contracts import CacheScope
from algen_agent_runtime.config.authentication import (
    AuthenticationAuditSettings,
    OidcSettings,
    TokenRevocationSettings,
    TrustedProxyIdentitySettings,
)
from algen_agent_runtime.exceptions.errors import ConfigurationError, PolicyDeniedError
from algen_agent_runtime.mcp.contracts import MCPServerConfig
from algen_agent_runtime.persistence.naming import check_schema_name
from algen_agent_runtime.persistence.quotas import StorageQuotaSettings
from algen_agent_runtime.retrieval.contracts import RetrievalMode, SourceDocument
from algen_agent_runtime.security.network import check_host_allowlist
from algen_agent_runtime.tools.http_api import HttpToolConfig
from algen_agent_runtime.types.contracts import AgentDefinition, ModelCapabilities
from algen_agent_runtime.workflows.contracts import WorkflowManifest, WorkflowResourceKind


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
    stream_usage: bool | None = Field(
        default=None,
        description="Request token usage on streamed calls (OpenAI-style stream_options); "
        "None keeps the adapter default.",
    )
    prompt_caching: bool = Field(
        default=True,
        description="Anthropic: cache the instructions and the conversation (cache_control); "
        "false for a gateway that rejects the field.",
    )

    @model_validator(mode="after")
    def reject_literal_secret(self) -> ProviderSettings:
        if self.api_key and not self.api_key.startswith(("env://", "secret://")):
            raise ValueError("api_key must be a secret reference (env:// or secret://)")
        return self


class StorageSettings(StrictSettings):
    run_store: Literal["memory", "postgres", "redis"] = "memory"
    memory_store: Literal["memory", "postgres", "redis"] = "memory"
    learned_memory_store: Literal["memory", "postgres"] = "memory"
    event_store: Literal["memory", "postgres"] = "memory"
    audit_store: Literal["memory", "postgres"] = "memory"
    approval_store: Literal["memory", "postgres"] = "memory"
    artifact_store: Literal["memory", "postgres", "s3"] = "memory"
    workflow_store: Literal["memory", "postgres"] = "memory"
    tool_execution_store: Literal["memory", "postgres"] = "memory"
    conversation_store: Literal["memory", "postgres"] = "memory"
    postgres_dsn: str | None = None
    postgres_schema: str | None = Field(
        default=None,
        description="PostgreSQL schema for the Runtime's tables, created when missing if "
        "initialize_schema is true. Unset: the server's search_path (normally public).",
    )
    redis_url: str | None = None
    artifact_s3_bucket: str | None = None
    artifact_s3_prefix: str = "algen-agent-runtime/artifacts"
    artifact_s3_region: str | None = None
    artifact_s3_endpoint_url: str | None = None
    artifact_s3_addressing_style: Literal["auto", "path", "virtual"] = "auto"
    artifact_s3_server_side_encryption: Literal["AES256", "aws:kms"] = "AES256"
    artifact_s3_kms_key_id: str | None = None
    initialize_schema: bool = True
    postgres_min_pool_size: int = Field(default=1, ge=1)
    postgres_max_pool_size: int = Field(default=10, ge=1)
    memory_retention_seconds: int = Field(default=86_400, ge=0)
    quotas: StorageQuotaSettings = Field(
        default_factory=StorageQuotaSettings,
        description="Per-tenant limits on stored artifacts, session memory and learned memory; "
        "unlimited by default.",
    )
    mcp_oauth_token_store: Literal["memory", "postgres"] = Field(
        default="memory",
        description="Where MCP OAuth tokens live. 'postgres' encrypts them at rest and needs "
        "mcp_oauth_encryption_key and the encryption extra.",
    )
    mcp_oauth_encryption_key: str | None = Field(
        default=None,
        description="env:// reference to a base64 32-byte AES-256-GCM key for stored MCP OAuth "
        "tokens.",
    )
    mcp_oauth_previous_encryption_keys: tuple[str, ...] = Field(
        default=(),
        description="env:// references to retired keys that may still decrypt stored tokens "
        "during a key rotation.",
    )

    @model_validator(mode="after")
    def validate_mcp_oauth_storage(self) -> StorageSettings:
        for reference in (self.mcp_oauth_encryption_key, *self.mcp_oauth_previous_encryption_keys):
            if reference is not None and not reference.startswith("env://"):
                raise ValueError("MCP OAuth encryption keys must be env:// secret references")
        if self.mcp_oauth_token_store == "postgres":
            if not self.postgres_dsn:
                raise ValueError("postgres-backed stores require postgres_dsn")
            if not self.mcp_oauth_encryption_key:
                raise ValueError(
                    "mcp_oauth_token_store 'postgres' requires mcp_oauth_encryption_key"
                )
        return self

    @model_validator(mode="after")
    def validate_backends(self) -> StorageSettings:
        selected = {
            self.run_store,
            self.memory_store,
            self.learned_memory_store,
            self.event_store,
            self.audit_store,
            self.approval_store,
            self.artifact_store,
            self.workflow_store,
            self.tool_execution_store,
            self.conversation_store,
        }
        if ("postgres" in selected or self.artifact_store == "s3") and not self.postgres_dsn:
            raise ValueError("postgres-backed stores require postgres_dsn")
        if "redis" in selected and not self.redis_url:
            raise ValueError("redis-backed stores require redis_url")
        for field_name in ("postgres_dsn", "redis_url"):
            value = getattr(self, field_name)
            if value and not value.startswith("env://"):
                raise ValueError(f"{field_name} must be an env:// secret reference")
        if self.postgres_min_pool_size > self.postgres_max_pool_size:
            raise ValueError("postgres_min_pool_size cannot exceed postgres_max_pool_size")
        if self.postgres_schema is not None:
            try:
                check_schema_name(self.postgres_schema)
            except ConfigurationError as exc:
                raise ValueError(str(exc)) from None
        if self.artifact_store == "s3" and not (self.artifact_s3_bucket or "").strip():
            raise ValueError("s3 artifact store requires artifact_s3_bucket")
        if self.artifact_store == "s3" and not self.artifact_s3_prefix.strip("/"):
            raise ValueError("artifact_s3_prefix cannot be empty")
        if self.artifact_store == "s3" and self.artifact_s3_endpoint_url:
            endpoint = urlparse(self.artifact_s3_endpoint_url)
            local_http = endpoint.scheme == "http" and endpoint.hostname in {
                "127.0.0.1",
                "localhost",
            }
            if endpoint.scheme != "https" and not local_http:
                raise ValueError("artifact_s3_endpoint_url must use HTTPS (HTTP is local-only)")
        if self.artifact_s3_kms_key_id and self.artifact_s3_server_side_encryption != "aws:kms":
            raise ValueError("artifact_s3_kms_key_id requires aws:kms server-side encryption")
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
    chunker: Literal["text", "markdown"] = Field(
        default="text",
        description="'markdown' splits documents at their headings and records each chunk's "
        "heading path and anchor; 'text' collapses whitespace and splits by size.",
    )
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
    export_whole_runs: bool = Field(
        default=True,
        description="Hold a run's spans until its root ends and export them together, so the "
        "backend never receives part of a run.",
    )
    run_export_max_spans: int = Field(default=2_000, ge=1, le=100_000)
    run_export_max_seconds: float = Field(default=300.0, gt=0, le=86_400)
    export_foreign_traces: bool = Field(
        default=False,
        description="Also export traces the Runtime did not start, such as the HTTP request spans "
        "a web framework or instrumentor creates through the global OpenTelemetry provider. By "
        "default Traccia receives only traces opened by a Runtime root.",
    )

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
    otlp_metrics_endpoint: str | None = Field(
        default=None,
        description="OTLP/HTTP metrics endpoint, e.g. https://collector:4318/v1/metrics. Unset: "
        "the Runtime's metrics are exported only by a meter provider installed elsewhere, such "
        "as Traccia's.",
    )
    otlp_metrics_export_interval_seconds: int = Field(default=60, ge=1, le=3_600)
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


class RouteLimitSettings(StrictSettings):
    rate_per_minute: int = Field(default=60, ge=1)
    burst: int = Field(default=10, ge=1)
    max_concurrent: int | None = Field(default=None, ge=1)


class ApiRateLimitSettings(StrictSettings):
    enabled: bool = False
    default_rate_per_minute: int = Field(default=120, ge=1)
    default_burst: int = Field(default=30, ge=1)
    max_concurrent_run_requests_per_tenant: int = Field(default=10, ge=1)
    fail_closed: bool = True
    route_overrides: dict[str, RouteLimitSettings] = Field(default_factory=dict)


class ApiSettings(StrictSettings):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    auth_mode: Literal["development_headers", "jwt", "oidc"] = "development_headers"
    allow_insecure_development_auth: bool = False
    jwt_key: str | None = Field(default=None, description="env:// signing or verification key")
    jwt_algorithms: tuple[str, ...] = ("RS256",)
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    tenant_claim: str = "tenant_id"
    scopes_claim: str = "scope"
    oidc: OidcSettings = Field(default_factory=OidcSettings)
    token_revocation: TokenRevocationSettings = Field(default_factory=TokenRevocationSettings)
    trusted_proxy_identity: TrustedProxyIdentitySettings = Field(
        default_factory=TrustedProxyIdentitySettings
    )
    authentication_audit: AuthenticationAuditSettings = Field(
        default_factory=AuthenticationAuditSettings
    )
    cors_allowed_origins: tuple[str, ...] = ()
    cors_allow_credentials: bool = False
    cors_allowed_headers: tuple[str, ...] = (
        "Authorization",
        "Content-Type",
        "Last-Event-ID",
        "X-Tenant-ID",
        "X-User-ID",
        "X-Scopes",
        "X-Artifact-Metadata",
    )
    rate_limiting: ApiRateLimitSettings = Field(default_factory=ApiRateLimitSettings)
    agent_ui_enabled: bool = False
    agent_ui_path: str = Field(default="/agent-ui", pattern=r"^/[a-zA-Z0-9_\-\./]*$")

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
        if any(algorithm.lower() == "none" for algorithm in self.jwt_algorithms):
            raise ValueError("jwt_algorithms may not include 'none'")
        if self.auth_mode == "oidc" and not self.oidc.issuers:
            raise ValueError("oidc authentication requires at least one oidc issuer")
        if self.auth_mode == "development_headers" and (
            self.token_revocation.enabled or self.trusted_proxy_identity.enabled
        ):
            raise ValueError(
                "token_revocation and trusted_proxy_identity require jwt or oidc authentication"
            )
        return self


READINESS_DEPENDENCY_KINDS = (
    "store",
    "cache",
    "provider",
    "retriever",
    "mcp",
    "telemetry",
    "resource",
)


class ReadinessSettings(StrictSettings):
    """Which dependencies ``/health/ready`` requires; every known dependency is reported.

    Dependencies are named ``<kind>.<name>``: ``store.run``, ``store.workflow``, ``cache.redis``,
    ``provider.openai``, ``retriever.docs``, ``mcp.crm``, ``telemetry.otlp`` or
    ``resource.<TypeName>`` for a plugin resource with ``health()`` or ``ping()``. Readiness fails
    (503) only when a required dependency is down; an optional one that is down makes the
    status ``degraded`` with HTTP 200.
    """

    model_providers: Literal["any", "all", "none"] = Field(
        default="any",
        description="How many registered model providers must be healthy: at least one, every "
        "one, or none (providers are still reported).",
    )
    require_durable_stores: bool = Field(
        default=True,
        description="Require every store configured on PostgreSQL, Redis or S3, and every plugin "
        "resource that exposes health() or ping(). In-memory stores are always up.",
    )
    required: tuple[str, ...] = Field(
        default=(),
        description="Further dependencies that must be up, for example mcp.crm, retriever.docs "
        "or provider.openai. MCP servers with required: true are always required.",
    )
    optional: tuple[str, ...] = Field(
        default=(),
        description="Dependencies to report without requiring them, for example cache.redis.",
    )
    probe_timeout_seconds: float = Field(
        default=2.0,
        gt=0,
        le=30,
        description="Upper bound for each dependency probe; a probe that takes longer is down.",
    )

    @model_validator(mode="after")
    def validate_names(self) -> ReadinessSettings:
        for name in (*self.required, *self.optional):
            kind, _, rest = name.partition(".")
            if kind not in READINESS_DEPENDENCY_KINDS or not rest:
                raise ValueError(
                    f"readiness dependency {name!r} must be named <kind>.<name> with kind one of "
                    f"{', '.join(READINESS_DEPENDENCY_KINDS)}"
                )
        both = sorted(set(self.required) & set(self.optional))
        if both:
            raise ValueError(f"readiness dependencies cannot be required and optional: {both}")
        return self


class RuntimeSettings(StrictSettings):
    component_check: Literal["error", "warn"] = Field(
        default="error",
        description="What build_container does when an agent names a tool, planner, composer, "
        "verifier, context builder or provider that nothing registered: fail, or log a warning.",
    )
    recover_incomplete_runs: bool = True
    recovery_limit: int = Field(default=1000, ge=1, le=100_000)
    shutdown_grace_seconds: float = Field(default=10.0, ge=0, le=300)
    recover_service_workflows: bool = Field(
        default=True,
        description="On start, recover workflow runs that the HTTP workflow service started "
        "(requires recover_incomplete_runs). Applications that drive workflows with their own "
        "executor keep calling executor.recover themselves.",
    )
    readiness: ReadinessSettings = Field(default_factory=ReadinessSettings)
    stream_delta_interval_ms: int = Field(
        default=50,
        ge=0,
        le=5_000,
        description="Streamed model text is recorded as at most one model.delta run event per "
        "interval (chunks in between are joined), so a long stream does not use up "
        "budget.max_events; 0 records every chunk.",
    )


class MaintenanceSettings(StrictSettings):
    """Background housekeeping; record deletion beyond expired data is opt-in."""

    enabled: bool = False
    interval_seconds: int = Field(default=300, ge=30, le=86_400)
    batch_size: int = Field(default=500, ge=1, le=10_000)
    max_batches_per_sweep: int = Field(default=20, ge=1, le=1_000)
    expire_paused_runs: bool = True
    expire_paused_workflows: bool = Field(
        default=True,
        description="Expire approval waits of service-owned workflow runs past their expires_at "
        "(and input waits older than clarification_timeout_seconds, when set) through the "
        "workflow engine, so rejection handling and checkpoints stay correct.",
    )
    clarification_timeout_seconds: int | None = Field(default=None, ge=60)
    terminal_run_retention_days: int | None = Field(default=None, ge=1)
    audit_retention_days: int | None = Field(default=None, ge=1)
    conversation_retention_days: int | None = Field(
        default=None,
        ge=1,
        description="Delete conversations (with messages, events and feedback) idle this long, "
        "unless a turn is in progress or waiting.",
    )
    workflow_checkpoint_retention_days: int | None = Field(
        default=None,
        ge=1,
        description="Delete completed, failed and cancelled workflow runs not updated this long, "
        "unless their parent run is unfinished.",
    )
    analytical_graph_retention_days: int | None = Field(
        default=None,
        ge=1,
        description="Delete completed, failed and cancelled analytical graphs not updated this long.",
    )
    work_item_retention_days: int | None = Field(
        default=None,
        ge=1,
        description="Delete completed and failed work items not updated this long; their "
        "deduplication keys are released.",
    )


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


class ConversationStreamingSettings(StrictSettings):
    """How conversation turns stream model text as ``assistant.message.delta`` events."""

    enabled: bool = Field(
        default=True,
        description="Stream turns whose agent's models can all stream; off gives only the final message",
    )
    delta_interval_ms: int = Field(
        default=50,
        ge=0,
        le=5_000,
        description="At most one delta event per interval per turn; smaller deltas are coalesced",
    )
    delta_max_chars: int = Field(
        default=2_048,
        ge=16,
        le=65_536,
        description="Largest delta event; a fuller buffer is sent at once, in pieces of this size",
    )


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


class QuerySourceSettings(StrictSettings):
    """Secret-safe configuration for an application-owned governed query backend."""

    type: Literal["postgres"] = "postgres"
    connection_url: str = Field(description="env:// reference to a read-only database DSN")
    read_only: Literal[True] = True
    purpose: str
    authorization_tags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_connection_reference(self) -> QuerySourceSettings:
        if not self.connection_url.startswith("env://"):
            raise ValueError("query source connection_url must be an env:// secret reference")
        return self


class DistributedExecutionSettings(StrictSettings):
    enabled: bool = False
    queue_backend: Literal["memory", "postgres"] = "memory"
    lease_seconds: int = Field(default=30, ge=5, le=3600)
    worker_concurrency: int = Field(default=4, ge=1, le=128)


class WorkflowNotificationSettings(StrictSettings):
    """Where the workflow service reports runs that wait for someone, or end."""

    webhook_url: str = Field(min_length=8, max_length=2048)
    # env:// reference to the key that signs each event (X-Algen-Signature: sha256=<hex>).
    signing_secret: str | None = None
    events: tuple[str, ...] = (
        "workflow.awaiting_approval",
        "workflow.awaiting_input",
        "workflow.completed",
        "workflow.failed",
    )

    @model_validator(mode="after")
    def secure(self) -> WorkflowNotificationSettings:
        if not self.webhook_url.startswith("https://"):
            raise ValueError("workflow_notifications.webhook_url must be an https URL")
        if self.signing_secret and not self.signing_secret.startswith("env://"):
            raise ValueError("workflow_notifications.signing_secret must be an env:// reference")
        return self


class AppSettings(StrictSettings):
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)
    agents: tuple[AgentDefinition, ...] = ()
    workflows: dict[str, WorkflowManifest] = Field(default_factory=dict)
    workflow_notifications: WorkflowNotificationSettings | None = None
    storage: StorageSettings = Field(default_factory=StorageSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    retrieval: dict[str, RetrievalSettings] = Field(default_factory=dict)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    maintenance: MaintenanceSettings = Field(default_factory=MaintenanceSettings)
    conversation_presentation: ConversationPresentationSettings = Field(
        default_factory=ConversationPresentationSettings
    )
    conversation_followups: ConversationFollowupSettings = Field(
        default_factory=ConversationFollowupSettings
    )
    conversation_streaming: ConversationStreamingSettings = Field(
        default_factory=ConversationStreamingSettings
    )
    analytical_execution: AnalyticalExecutionSettings = Field(
        default_factory=AnalyticalExecutionSettings
    )
    query_governance: QueryGovernanceSettings = Field(default_factory=QueryGovernanceSettings)
    query_sources: dict[str, QuerySourceSettings] = Field(default_factory=dict)
    distributed_execution: DistributedExecutionSettings = Field(
        default_factory=DistributedExecutionSettings
    )
    mcp_servers: tuple[MCPServerConfig, ...] = ()
    # Declarative HTTP API tools; see algen_agent_runtime.tools.http_api.
    http_tools: tuple[HttpToolConfig, ...] = ()
    feature_flags: dict[str, bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_http_tools(self) -> AppSettings:
        names = [tool.name for tool in self.http_tools]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"http tool names must be unique: {duplicates}")
        reserved = sorted(name for name in names if name.startswith("core."))
        if reserved:
            raise ValueError(f"http tool names may not use the core. prefix: {reserved}")
        allowed = self.security.allowed_http_hosts
        for tool in self.http_tools:
            # An empty allowlist admits every host; declared API tools must be listed.
            try:
                if not allowed:
                    raise PolicyDeniedError("no hosts are allowlisted")
                check_host_allowlist(tool.host, allowed)
            except PolicyDeniedError as exc:
                raise ValueError(
                    f"http tool {tool.name!r} calls {tool.host!r}, which must be listed in "
                    "security.allowed_http_hosts"
                ) from exc
        return self

    @model_validator(mode="after")
    def validate_token_revocation_store(self) -> AppSettings:
        revocation = self.api.token_revocation
        if revocation.enabled and revocation.store == "postgres" and not self.storage.postgres_dsn:
            raise ValueError("api.token_revocation store 'postgres' requires storage.postgres_dsn")
        return self

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
        agent_names = {agent.name for agent in self.agents}
        for workflow_name, workflow in self.workflows.items():
            if workflow_name != workflow.name:
                raise ValueError(
                    f"workflow registry key {workflow_name!r} must match manifest name "
                    f"{workflow.name!r}"
                )
            missing_agents = {
                node.agent
                for node in workflow.nodes
                if node.agent is not None and node.agent not in agent_names
            }
            if missing_agents:
                raise ValueError(
                    f"workflow {workflow_name!r} references unknown agents {sorted(missing_agents)}"
                )
            for node in workflow.nodes:
                for resource in node.resources:
                    if (
                        resource.kind == WorkflowResourceKind.QUERY_SOURCE
                        and resource.name not in self.query_sources
                    ):
                        raise ValueError(
                            f"workflow node {node.id!r} references unknown query source "
                            f"{resource.name!r}"
                        )
                    if (
                        resource.kind == WorkflowResourceKind.RETRIEVAL
                        and resource.name not in self.retrieval
                    ):
                        raise ValueError(
                            f"workflow node {node.id!r} references unknown retrieval index "
                            f"{resource.name!r}"
                        )
                    if (
                        resource.kind == WorkflowResourceKind.CACHE
                        and resource.name not in self.cache.policies
                    ):
                        raise ValueError(
                            f"workflow node {node.id!r} references unknown cache policy "
                            f"{resource.name!r}"
                        )
        return self

    @model_validator(mode="after")
    def validate_provider_references(self) -> AppSettings:
        invalid_references: list[str] = []
        for agent in self.agents:
            # A plugin may register the provider; build_container checks that one did.
            def unknown(provider: str | None, plugins: bool = bool(agent.domain_plugins)) -> bool:
                return provider is None or (provider not in self.providers and not plugins)

            if unknown(agent.default_model.provider):
                invalid_references.append(
                    f"agents[{agent.name}].default_model.provider ({agent.default_model.provider!r})"
                )
            for idx, fallback in enumerate(agent.fallback_models):
                if unknown(fallback.provider):
                    invalid_references.append(
                        f"agents[{agent.name}].fallback_models[{idx}].provider ({fallback.provider!r})"
                    )
            preferences = [
                profile.routing_preference
                for profile in (agent.default_model, *agent.fallback_models)
            ]
            if "ordered" in preferences and set(preferences) != {"ordered"}:
                raise ValueError(
                    f"agents[{agent.name}]: routing_preference 'ordered' applies to the whole "
                    "model list; set it on default_model and every fallback_models entry, or on "
                    f"none (got {preferences})"
                )
        for retrieval_name, retrieval_config in self.retrieval.items():
            if (
                retrieval_config.embedding_provider is not None
                and retrieval_config.embedding_provider not in self.providers
            ):
                invalid_references.append(
                    f"retrieval[{retrieval_name}].embedding_provider ({retrieval_config.embedding_provider!r})"
                )
        if invalid_references:
            raise ValueError(
                f"invalid provider references: {'; '.join(invalid_references)} "
                f"(configured providers: {sorted(self.providers.keys())})"
            )
        mcp_names: set[str] = set()
        for server in self.mcp_servers:
            if server.name in mcp_names:
                raise ValueError(f"duplicate MCP server name {server.name!r}")
            mcp_names.add(server.name)
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
    *,
    ignore_extensions: bool = False,
) -> AppSettings:
    """Merge YAML files, ``ALGEN_AGENT_RUNTIME__`` variables and overrides into settings.

    ``ignore_extensions`` drops a top-level ``extensions`` mapping, where Studio keeps its own
    project metadata, instead of rejecting it.
    """
    data: dict[str, Any] = {}
    try:
        for file in files:
            loaded = yaml.safe_load(Path(file).read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ConfigurationError(f"configuration root in {file} must be a mapping")
            data = _merge(data, loaded)
        data = _merge(data, _environment("ALGEN_AGENT_RUNTIME__"))
        data = _merge(data, deployment_overrides or {})
        if ignore_extensions:
            data.pop("extensions", None)
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

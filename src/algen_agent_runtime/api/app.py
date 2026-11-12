from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, cast

from algen_agent_runtime.extras import MissingExtraError

try:
    import uvicorn
    from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
except ModuleNotFoundError as exc:
    raise MissingExtraError("The HTTP API", "server", missing=exc.name) from exc
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from algen_agent_runtime import __version__
from algen_agent_runtime.api.auth_routes import add_auth_routes
from algen_agent_runtime.api.body_limit import (
    RequestBodyLimitMiddleware,
    RequestPayloadTooLarge,
)
from algen_agent_runtime.api.dependencies import (
    Principal,
    principal_dependency,
    require_any_scope,
    require_scope,
)
from algen_agent_runtime.api.mcp_oauth_routes import add_mcp_oauth_routes
from algen_agent_runtime.api.rate_limiting import (
    ROUTE_CLASS_READ,
    ROUTE_CLASS_RUN_CREATE,
    ROUTE_CLASS_WRITE,
    InMemoryApiRateLimiter,
    RouteLimitPolicy,
)
from algen_agent_runtime.api.tool_execution_routes import add_tool_execution_routes
from algen_agent_runtime.api.user_data_routes import add_user_data_routes
from algen_agent_runtime.api.workflow_routes import add_workflow_routes
from algen_agent_runtime.approvals.service import (
    MAX_APPROVAL_OFFSET,
    MAX_APPROVAL_PAGE,
    MAX_COMMENT_CHARS,
    ApprovalRequest,
    ApprovalStatus,
)
from algen_agent_runtime.artifacts import ArtifactLifecycleService, BasicArtifactScanner
from algen_agent_runtime.config.settings import ApiSettings, AppSettings, load_settings
from algen_agent_runtime.conversations.contracts import (
    ConversationApprovalDecision,
    ConversationCredentialsRenewal,
    ConversationEvent,
    ConversationInputAnswer,
    ConversationStatus,
)
from algen_agent_runtime.conversations.feedback import FeedbackRating
from algen_agent_runtime.events.contracts import (
    RUN_END_EVENT_TYPES,
    STREAM_END_EVENT_TYPES,
    AuditEvent,
    RunEvent,
)
from algen_agent_runtime.exceptions.errors import (
    AlgenAgentRuntimeError,
    ConfigurationError,
    ConflictError,
    EventStreamOverflowError,
    NotFoundError,
    QuotaExceededError,
    StoreUnavailableError,
)
from algen_agent_runtime.observability.setup import (
    configure_logging,
    configure_telemetry,
    flush_telemetry,
)
from algen_agent_runtime.orchestration.container import Container, build_container
from algen_agent_runtime.security.redaction import redact_parameters
from algen_agent_runtime.types.contracts import (
    MAX_STEERING_CHARS,
    AgentDefinition,
    Artifact,
    ArtifactStatus,
    RequestOverrides,
    RunAttachment,
    RunRequest,
    utc_now,
)
from algen_agent_runtime.workflows.service import WorkflowService


class CreateRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str
    agent_version: str | None = None
    input: str = Field(min_length=1)
    # Uploaded files (POST /v1/artifacts) the agent reads with the input; it must accept them.
    attachments: tuple[RunAttachment, ...] = Field(default=(), max_length=32)
    session_id: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    stream: bool = False
    metadata: dict[str, str] = Field(default_factory=dict)
    overrides: RequestOverrides = Field(default_factory=RequestOverrides)


class ClarificationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clarification: str = Field(min_length=1)


class SteerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=MAX_STEERING_CHARS)


class ApprovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str
    modified_parameters: dict[str, Any] | None = None
    # The approver's note, recorded on the approval request with who decided and when.
    comment: str | None = Field(default=None, max_length=MAX_COMMENT_CHARS)


class ArtifactStatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ArtifactStatus


class CreateConversationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str = Field(min_length=1)
    # Other agents a turn may choose with the message's ``agent``.
    allowed_agents: tuple[str, ...] = Field(default=(), max_length=32)
    handler: str = "runtime"
    title: str = Field(default="New conversation", min_length=1, max_length=160)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateConversationMessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=100_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    attachments: tuple[RunAttachment, ...] = Field(default=(), max_length=32)
    # The agent for this turn: the conversation's or one of its allowed_agents.
    agent: str | None = Field(default=None, min_length=1)


class UpdateConversationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=160)
    status: ConversationStatus | None = None

    @model_validator(mode="after")
    def require_update(self) -> UpdateConversationBody:
        if self.title is None and self.status is None:
            raise ValueError("title or status is required")
        return self


class ConversationFeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rating: FeedbackRating
    category: str | None = Field(default=None, max_length=100)
    comment: str | None = Field(default=None, max_length=2_000)
    tags: tuple[str, ...] = Field(default=(), max_length=20)


def _sse(event: RunEvent) -> bytes:
    payload = event.model_dump_json()
    return f"id: {event.sequence}\nevent: {event.type}\ndata: {payload}\n\n".encode()


# How often a run event stream that follows a run through its pauses sends a keepalive comment.
_SSE_KEEPALIVE_SECONDS = 15.0


def _sse_overflow(last_sequence: int) -> bytes:
    """The last frame of a run event stream whose subscriber fell behind: an ``id`` of the last
    event delivered, so a client reconnecting with Last-Event-ID misses nothing."""
    payload = json.dumps({"last_sequence": last_sequence, "reconnect": True})
    return f"id: {last_sequence}\nevent: stream.overflow\ndata: {payload}\nretry: 1000\n\n".encode()


def _conversation_sse(event: ConversationEvent) -> bytes:
    return (
        f"id: {event.sequence}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"
    ).encode()


def _check_api_settings(api: ApiSettings) -> None:
    """Validate the API settings again before serving them.

    ``model_copy(update=...)`` skips validation, so settings changed that way (a host override,
    say) could otherwise serve development-header authentication on a non-loopback host.
    """
    try:
        ApiSettings.model_validate(api.model_dump())
    except ValidationError as exc:
        reasons = "; ".join(
            str(error["msg"]).removeprefix("Value error, ") for error in exc.errors()
        )
        raise ConfigurationError(f"invalid API settings: {reasons}") from None


def create_app(
    settings: AppSettings | None = None,
    container: Container | None = None,
) -> FastAPI:
    resolved = settings or AppSettings()
    _check_api_settings(resolved.api)
    dependencies = container or build_container(resolved)
    credential_headers = resolved.api.credential_headers

    def caller_credentials(request: Request) -> dict[str, Any]:
        """``credentials=`` with the configured headers' values, for the run's tools
        (``api.credential_headers``), or no keyword at all when none are configured or sent.
        Memory only, never stored."""
        values = {
            name: request.headers[name] for name in credential_headers if name in request.headers
        }
        return {"credentials": values} if values else {}

    authenticate = principal_dependency(
        resolved.api,
        audits=dependencies.runtime.audits,
        revocations=dependencies.token_revocations,
        oidc_transport=dependencies.oidc_transport,
        outbound=dependencies.outbound,
    )
    identity_dependency = Depends(authenticate)
    configure_logging()

    workflows = WorkflowService(resolved, dependencies)
    # The container recovers, expires and drains the runs this service starts.
    dependencies.attach_workflows(workflows)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        dependencies.observability.start()
        configure_telemetry(resolved.telemetry)
        app.state.container = dependencies
        try:
            await dependencies.astart()
            yield
        finally:
            # Drains the workflow service first (attach_workflows), then the runtime.
            await dependencies.aclose()
            await workflows.aclose()
            flush_telemetry()

    app = FastAPI(title="Algen Agent Runtime", version=__version__, lifespan=lifespan)
    app.state.container = dependencies
    if resolved.api.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.api.cors_allowed_origins),
            allow_credentials=resolved.api.cors_allow_credentials,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=list(resolved.api.cors_allowed_headers),
        )

    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_request_bytes=resolved.security.max_request_bytes,
        max_artifact_bytes=resolved.security.max_artifact_bytes,
    )

    rate_limiter = dependencies.rate_limiter
    if rate_limiter is None and resolved.api.rate_limiting.enabled:
        route_policies = {
            name: RouteLimitPolicy(
                rate_per_minute=policy.rate_per_minute,
                burst=policy.burst,
                max_concurrent=policy.max_concurrent,
            )
            for name, policy in resolved.api.rate_limiting.route_overrides.items()
        }
        rate_limiter = InMemoryApiRateLimiter(
            default_rate_per_minute=resolved.api.rate_limiting.default_rate_per_minute,
            default_burst=resolved.api.rate_limiting.default_burst,
            max_concurrent_run_requests_per_tenant=resolved.api.rate_limiting.max_concurrent_run_requests_per_tenant,
            route_policies=route_policies,
            fail_closed=resolved.api.rate_limiting.fail_closed,
        )

    if rate_limiter is not None:

        @app.middleware("http")
        async def admission_rate_limit(request: Request, call_next: Any) -> Response:
            path = request.url.path.rstrip("/")
            ui_path_clean = resolved.api.agent_ui_path.rstrip("/")
            if (
                path
                in {
                    "/health/live",
                    "/health/ready",
                    "/healthz",
                    "/ready",
                    "/health",
                    "/ui",
                    ui_path_clean,
                }
                or path.startswith(f"{ui_path_clean}/")
                or path.startswith("/ui/")
                or request.method == "OPTIONS"
            ):
                return cast(Response, await call_next(request))

            if request.method == "POST" and path == "/v1/runs":
                route_class = ROUTE_CLASS_RUN_CREATE
            elif request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                route_class = ROUTE_CLASS_WRITE
            else:
                route_class = ROUTE_CLASS_READ

            try:
                identity = await authenticate(request)
            except HTTPException:
                return cast(Response, await call_next(request))

            decision = await rate_limiter.acquire(
                tenant_id=identity.tenant_id,
                route_class=route_class,
                principal_id=identity.user_id,
            )
            if not decision.allowed:
                retry_seconds = max(1, math.ceil(decision.retry_after_seconds))
                code = (
                    "CONCURRENCY_LIMIT_EXCEEDED"
                    if decision.reason == "concurrency_limit_exceeded"
                    else "RATE_LIMIT_EXCEEDED"
                )
                return JSONResponse(
                    status_code=429,
                    headers={"Retry-After": str(retry_seconds)},
                    content={
                        "detail": (
                            f"Rate limit exceeded for route class '{route_class}'. "
                            f"Please retry after {retry_seconds} seconds."
                        ),
                        "code": code,
                        "retry_after": retry_seconds,
                        "tenant_id": identity.tenant_id,
                        "route_class": route_class,
                    },
                )

            try:
                return cast(Response, await call_next(request))
            finally:
                await rate_limiter.release(
                    tenant_id=identity.tenant_id,
                    route_class=route_class,
                    principal_id=identity.user_id,
                )

    @app.exception_handler(RequestPayloadTooLarge)
    async def payload_too_large(request: Request, exc: RequestPayloadTooLarge) -> JSONResponse:
        return JSONResponse(
            status_code=413,
            content={
                "detail": "request payload too large",
                "code": "REQUEST_PAYLOAD_TOO_LARGE",
                "limit_bytes": exc.limit,
            },
        )

    @app.exception_handler(QuotaExceededError)
    async def quota_exceeded(request: Request, exc: QuotaExceededError) -> JSONResponse:
        # 413 like an oversized request: retrying the same write cannot succeed until the
        # tenant deletes data or its quota is raised.
        return JSONResponse(
            status_code=413,
            content={
                "detail": str(exc),
                "code": "QUOTA_EXCEEDED",
                "resource": exc.resource,
                "dimension": exc.dimension,
                "limit": exc.limit,
                "used": exc.used,
            },
        )

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict(request: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(StoreUnavailableError)
    async def store_unavailable(request: Request, exc: StoreUnavailableError) -> JSONResponse:
        # A store outage, not a bad request: the client may retry once the store is back.
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc), "kind": exc.error_kind.value},
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(AlgenAgentRuntimeError)
    async def core_error(request: Request, exc: AlgenAgentRuntimeError) -> JSONResponse:
        return JSONResponse(
            status_code=400, content={"detail": str(exc), "kind": exc.error_kind.value}
        )

    add_workflow_routes(app, workflows, identity_dependency)
    add_mcp_oauth_routes(app, dependencies, identity_dependency)
    add_auth_routes(app, dependencies, identity_dependency)
    add_tool_execution_routes(app, dependencies, identity_dependency)
    add_user_data_routes(app, dependencies, identity_dependency)

    async def require_conversation_owner(run_id: str, identity: Principal) -> None:
        """A run started for a conversation turn is its owner's, as on the conversation routes:
        another user of the tenant gets the same 404 as for an unknown run."""
        state = await dependencies.runtime.status(run_id, identity.tenant_id)
        conversation_id = state.request.conversation_id
        if conversation_id is None:
            return
        try:
            conversation = await dependencies.conversations.get(conversation_id, identity.tenant_id)
            owner = conversation.user_id
        except NotFoundError:  # the conversation was deleted; the run keeps its user
            owner = state.request.user_id
        if owner != identity.user_id:
            raise NotFoundError(f"run {run_id!r} not found")

    async def require_run_user(
        run_id: str, identity: Principal, action: str, *, override_scope: str | None = None
    ) -> None:
        """Acting on a run (deciding its approval, cancelling, steering or answering it) is
        its user's: the conversation's owner for a conversation turn, else the user who started
        it. ``override_scope``, when given and held, lets an operator act on any run of the
        tenant (an approver with ``approvals:decide``, say)."""
        if override_scope is not None and (
            override_scope in identity.scopes or "*" in identity.scopes
        ):
            await dependencies.runtime.status(run_id, identity.tenant_id)
            return
        await require_conversation_owner(run_id, identity)
        state = await dependencies.runtime.status(run_id, identity.tenant_id)
        if state.request.conversation_id is None and state.request.user_id != identity.user_id:
            allowed = f" or a principal with scope {override_scope}" if override_scope else ""
            raise HTTPException(
                status_code=403,
                detail=f"only the run's user{allowed} may {action} it",
            )

    def approval_view(approval: ApprovalRequest) -> dict[str, Any]:
        """An approval as the inbox routes return it. ``redacted_parameters`` were redacted
        before they were stored; ``modified_parameters`` are stored as the approver sent them,
        so they are redacted here the same way."""
        if approval.modified_parameters is not None:
            approval = approval.model_copy(
                update={"modified_parameters": redact_parameters(approval.modified_parameters)}
            )
        return approval.model_dump(mode="json")

    @app.post("/v1/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(
        body: CreateRunBody, request: Request, identity: Principal = identity_dependency
    ) -> dict[str, Any]:
        require_scope(identity, "runs:write")
        state = await dependencies.runtime.start(
            RunRequest(**body.model_dump(), tenant_id=identity.tenant_id, user_id=identity.user_id),
            **caller_credentials(request),
        )
        return {"run_id": state.id, "session_id": state.session_id, "status": state.status}

    @app.get("/v1/runs/{run_id}")
    async def read_run(run_id: str, identity: Principal = identity_dependency) -> dict[str, Any]:
        require_scope(identity, "runs:read")
        await require_conversation_owner(run_id, identity)
        state = await dependencies.runtime.status(run_id, identity.tenant_id)
        return state.model_dump(
            mode="json", exclude={"messages", "pending_tool_calls", "pending_steering"}
        )

    @app.get("/v1/runs/{run_id}/narrative")
    async def read_run_narrative(
        run_id: str, identity: Principal = identity_dependency
    ) -> dict[str, Any]:
        require_scope(identity, "runs:read")
        await require_conversation_owner(run_id, identity)
        narrative = await dependencies.runtime.narrative(run_id, identity.tenant_id)
        return narrative.model_dump(mode="json")

    @app.delete("/v1/runs/{run_id}")
    async def cancel_run(run_id: str, identity: Principal = identity_dependency) -> dict[str, Any]:
        require_scope(identity, "runs:write")
        await require_run_user(run_id, identity, "cancel", override_scope="runs:admin")
        state = await dependencies.runtime.cancel(run_id, identity.tenant_id)
        return {"run_id": state.id, "status": state.status}

    @app.post("/v1/runs/{run_id}/clarification", status_code=202)
    async def clarify_run(
        run_id: str,
        body: ClarificationBody,
        request: Request,
        identity: Principal = identity_dependency,
    ) -> dict[str, Any]:
        require_scope(identity, "runs:write")
        # The answer goes to the model as the user's: only the run's user may give it.
        await require_run_user(run_id, identity, "answer")
        state = await dependencies.runtime.resume(
            run_id,
            identity.tenant_id,
            body.model_dump(),
            **caller_credentials(request),
        )
        return {"run_id": state.id, "status": state.status}

    @app.post("/v1/runs/{run_id}/steer", status_code=202)
    async def steer_run(
        run_id: str, body: SteerBody, identity: Principal = identity_dependency
    ) -> dict[str, Any]:
        require_scope(identity, "runs:write")
        # Steering puts text in front of the model as the user: only the run's user may.
        await require_run_user(run_id, identity, "steer")
        try:
            message = await dependencies.runtime.steer(run_id, identity.tenant_id, body.text)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"run_id": run_id, "steer_id": message.id, "status": "queued"}

    @app.post("/v1/runs/{run_id}/approval", status_code=202)
    async def approve_run(
        run_id: str,
        body: ApprovalBody,
        request: Request,
        identity: Principal = identity_dependency,
    ) -> dict[str, Any]:
        require_scope(identity, "runs:write")
        await require_run_user(run_id, identity, "decide", override_scope="approvals:decide")
        # The approver is the authenticated user, never a field of the body.
        state = await dependencies.runtime.resume(
            run_id,
            identity.tenant_id,
            body.model_dump(),
            **caller_credentials(request),
            decided_by=identity.user_id,
        )
        return {"run_id": state.id, "status": state.status}

    @app.get("/v1/approvals")
    async def list_approvals(
        approval_status: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
        run_id: str | None = None,
        agent: str | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_APPROVAL_PAGE)] = 50,
        offset: Annotated[int, Query(ge=0, le=MAX_APPROVAL_OFFSET)] = 0,
        identity: Principal = identity_dependency,
    ) -> list[dict[str, Any]]:
        # Every user's pending actions in the tenant, conversation turns included: a grant an
        # operator gives on purpose, not a side effect of runs:read.
        require_scope(identity, "approvals:read")
        approvals = await dependencies.runtime.approvals.list(
            identity.tenant_id,
            status=approval_status,
            run_id=run_id,
            agent=agent,
            limit=limit,
            offset=offset,
        )
        return [approval_view(approval) for approval in approvals]

    @app.get("/v1/approvals/{approval_id}")
    async def read_approval(
        approval_id: str, identity: Principal = identity_dependency
    ) -> dict[str, Any]:
        require_scope(identity, "approvals:read")
        approval = await dependencies.runtime.approvals.get(approval_id, identity.tenant_id)
        if approval is None:
            raise NotFoundError("approval request not found")
        return approval_view(approval.effective(utc_now()))

    @app.get("/v1/storage/usage")
    async def storage_usage(identity: Principal = identity_dependency) -> dict[str, Any]:
        require_scope(identity, "usage:read")
        service = dependencies.storage_usage
        if service is None:
            raise HTTPException(status_code=404, detail="storage usage is not available")
        return (await service.usage(identity.tenant_id)).model_dump(mode="json")

    @app.get("/v1/runs/{run_id}/events")
    async def stream_events(
        run_id: str,
        request: Request,
        through_pauses: bool = False,
        identity: Principal = identity_dependency,
    ) -> StreamingResponse:
        require_any_scope(identity, "artifacts:read", "runs:read")
        await require_conversation_owner(run_id, identity)
        last_id = int(request.headers.get("last-event-id", "0") or 0)
        # By default the stream ends when the run ends or pauses, and after answering a pause
        # the client reconnects with Last-Event-ID; ?through_pauses=true ends only when the run
        # ends, sending a keepalive comment while nothing happens (a pause can last hours).
        ends = RUN_END_EVENT_TYPES if through_pauses else STREAM_END_EVENT_TYPES
        keepalive = _SSE_KEEPALIVE_SECONDS if through_pauses else None

        async def generate() -> AsyncIterator[bytes]:
            delivered = last_id
            iterator = dependencies.events.subscribe(run_id, after=last_id).__aiter__()
            pending: asyncio.Future[RunEvent] = asyncio.ensure_future(iterator.__anext__())
            try:
                while True:
                    done, _ = await asyncio.wait((pending,), timeout=keepalive)
                    if not done:
                        yield b": keepalive\n\n"
                        continue
                    try:
                        event = pending.result()
                    except StopAsyncIteration:
                        return
                    except EventStreamOverflowError as exc:
                        # Too slow for the live queue: tell the client where to reconnect from.
                        yield _sse_overflow(exc.last_sequence)
                        return
                    pending = asyncio.ensure_future(iterator.__anext__())
                    if event.sequence <= delivered:
                        continue
                    delivered = event.sequence
                    yield _sse(event)
                    if event.type in ends:
                        return
            finally:
                pending.cancel()
                close = getattr(iterator, "aclose", None)
                if close is not None:
                    with contextlib.suppress(Exception):
                        await close()

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/artifacts", status_code=status.HTTP_201_CREATED)
    async def create_artifact(
        request: Request,
        name: str,
        media_type: str = "application/octet-stream",
        run_id: str | None = None,
        expires_in_seconds: int | None = None,
        identity: Principal = identity_dependency,
    ) -> dict[str, Any]:
        require_any_scope(identity, "artifacts:write", "runs:write")
        if not name.strip() or len(name) > 255 or any(char in name for char in "\r\n"):
            raise HTTPException(status_code=422, detail="invalid artifact name")
        if expires_in_seconds is not None and not 60 <= expires_in_seconds <= 31_536_000:
            raise HTTPException(
                status_code=422,
                detail="expires_in_seconds must be between 60 and 31536000",
            )
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > resolved.security.max_artifact_bytes:
                raise RequestPayloadTooLarge(resolved.security.max_artifact_bytes)
        metadata_header = request.headers.get("x-artifact-metadata", "{}")
        try:
            metadata = json.loads(metadata_header)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail="invalid x-artifact-metadata JSON") from exc
        if not isinstance(metadata, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in metadata.items()
        ):
            raise HTTPException(
                status_code=422, detail="artifact metadata must be a string-to-string object"
            )
        artifact = Artifact(
            tenant_id=identity.tenant_id,
            run_id=run_id,
            name=name.strip(),
            media_type=media_type,
            data=bytes(data),
            status=ArtifactStatus.PENDING_SCAN,
            metadata=metadata,
            expires_at=(
                utc_now() + timedelta(seconds=expires_in_seconds)
                if expires_in_seconds is not None
                else None
            ),
        )
        await dependencies.artifacts.put(artifact)
        if resolved.security.upload_scanner == "basic":
            scanned = await ArtifactLifecycleService(
                dependencies.artifacts, dependencies.runtime.audits
            ).scan(
                artifact.id, identity.tenant_id, "runtime.upload_scanner", BasicArtifactScanner()
            )
            return scanned.model_dump(mode="json")
        return artifact.descriptor().model_dump(mode="json")

    @app.get("/v1/artifacts")
    async def list_artifacts(
        run_id: str | None = None,
        artifact_status: ArtifactStatus | None = None,
        limit: int = 100,
        identity: Principal = identity_dependency,
    ) -> list[dict[str, Any]]:
        require_any_scope(identity, "artifacts:read", "runs:read")
        if not 1 <= limit <= 1000:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 1000")
        artifacts = await dependencies.artifacts.list(
            identity.tenant_id, run_id=run_id, status=artifact_status, limit=limit
        )
        return [artifact.model_dump(mode="json") for artifact in artifacts]

    @app.get("/v1/artifacts/{artifact_id}/metadata")
    async def read_artifact_metadata(
        artifact_id: str, identity: Principal = identity_dependency
    ) -> dict[str, Any]:
        require_any_scope(identity, "artifacts:read", "runs:read")
        artifact = await dependencies.artifacts.describe(artifact_id, identity.tenant_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        return artifact.model_dump(mode="json")

    @app.patch("/v1/artifacts/{artifact_id}/status")
    async def update_artifact_status(
        artifact_id: str,
        body: ArtifactStatusBody,
        identity: Principal = identity_dependency,
    ) -> dict[str, Any]:
        require_scope(identity, "artifacts:scan")
        artifact = await dependencies.artifacts.set_status(
            artifact_id, identity.tenant_id, body.status
        )
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        await dependencies.runtime.audits.append(
            AuditEvent(
                action="artifact.status.override",
                outcome=body.status.value,
                tenant_id=identity.tenant_id,
                actor_id=identity.user_id,
                resource_id=artifact_id,
                metadata={"sha256": artifact.sha256, "size_bytes": artifact.size_bytes},
            )
        )
        return artifact.model_dump(mode="json")

    @app.delete("/v1/artifacts/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_artifact(
        artifact_id: str, identity: Principal = identity_dependency
    ) -> Response:
        require_any_scope(identity, "artifacts:write", "runs:write")
        if not await dependencies.artifacts.delete(artifact_id, identity.tenant_id):
            raise HTTPException(status_code=404, detail="artifact not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/v1/artifacts/{artifact_id}")
    async def read_artifact(
        artifact_id: str, identity: Principal = identity_dependency
    ) -> Response:
        require_any_scope(identity, "artifacts:read", "runs:read")
        artifact = await dependencies.artifacts.get(artifact_id, identity.tenant_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        if artifact.status is not ArtifactStatus.AVAILABLE:
            raise HTTPException(
                status_code=409,
                detail=f"artifact is not available ({artifact.status.value})",
            )
        descriptor = artifact.descriptor()
        return Response(
            artifact.data,
            media_type=artifact.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{artifact.name}"',
                "X-Artifact-Sha256": descriptor.sha256,
                "X-Artifact-Status": artifact.status.value,
            },
        )

    @app.post("/v1/conversations", status_code=status.HTTP_201_CREATED)
    async def create_conversation(
        body: CreateConversationBody, identity: Principal = identity_dependency
    ) -> dict[str, Any]:
        require_scope(identity, "conversations:write")
        conversation = await dependencies.conversations.create(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            agent=body.agent,
            handler=body.handler,
            title=body.title,
            metadata=body.metadata,
            allowed_agents=body.allowed_agents,
        )
        return conversation.model_dump(mode="json")

    @app.get("/v1/conversations")
    async def list_conversations(
        limit: int = 50,
        identity: Principal = identity_dependency,
    ) -> list[dict[str, Any]]:
        require_scope(identity, "conversations:read")
        items = await dependencies.conversations.list(
            identity.tenant_id, identity.user_id, min(max(limit, 1), 100)
        )
        return [item.model_dump(mode="json") for item in items]

    @app.get("/v1/conversations/suggestions")
    async def conversation_suggestions(
        handler: str = "runtime",
        limit: int = 5,
        identity: Principal = identity_dependency,
    ) -> dict[str, Any]:
        require_scope(identity, "conversations:read")
        suggestions = await dependencies.conversations.suggestions(
            handler, identity.tenant_id, identity.user_id, min(max(limit, 1), 10)
        )
        return {"handler": handler, "suggestions": suggestions}

    @app.get("/v1/conversations/{conversation_id}")
    async def read_conversation(
        conversation_id: str, identity: Principal = identity_dependency
    ) -> dict[str, Any]:
        require_scope(identity, "conversations:read")
        conversation = await dependencies.conversations.get(conversation_id, identity.tenant_id)
        if conversation.user_id != identity.user_id:
            raise NotFoundError(f"conversation {conversation_id!r} not found")
        return conversation.model_dump(mode="json")

    @app.patch("/v1/conversations/{conversation_id}")
    async def update_conversation(
        conversation_id: str,
        body: UpdateConversationBody,
        identity: Principal = identity_dependency,
    ) -> dict[str, Any]:
        require_scope(identity, "conversations:write")
        conversation = await dependencies.conversations.update(
            conversation_id,
            identity.tenant_id,
            identity.user_id,
            title=body.title,
            status=body.status,
        )
        return conversation.model_dump(mode="json")

    @app.delete("/v1/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(
        conversation_id: str, identity: Principal = identity_dependency
    ) -> Response:
        require_scope(identity, "conversations:write")
        await dependencies.conversations.delete(
            conversation_id, identity.tenant_id, identity.user_id
        )
        return Response(status_code=204)

    @app.get("/v1/conversations/{conversation_id}/messages")
    async def read_conversation_messages(
        conversation_id: str,
        limit: int = 100,
        identity: Principal = identity_dependency,
    ) -> list[dict[str, Any]]:
        require_scope(identity, "conversations:read")
        conversation = await dependencies.conversations.get(conversation_id, identity.tenant_id)
        if conversation.user_id != identity.user_id:
            raise NotFoundError(f"conversation {conversation_id!r} not found")
        items = await dependencies.conversations.messages(
            conversation_id, identity.tenant_id, min(max(limit, 1), 500)
        )
        return [item.model_dump(mode="json") for item in items]

    @app.post(
        "/v1/conversations/{conversation_id}/messages",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_conversation_message(
        conversation_id: str,
        body: CreateConversationMessageBody,
        request: Request,
        identity: Principal = identity_dependency,
    ) -> dict[str, Any]:
        require_scope(identity, "conversations:write")
        try:
            user_message, assistant_message = await dependencies.conversations.submit(
                conversation_id,
                identity.tenant_id,
                identity.user_id,
                body.text,
                body.metadata,
                attachments=body.attachments,
                **caller_credentials(request),
                agent=body.agent,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "message_id": user_message.id,
            "assistant_message_id": assistant_message.id,
            "status": assistant_message.status,
        }

    @app.delete("/v1/conversations/{conversation_id}/active-response", status_code=202)
    async def cancel_conversation_response(
        conversation_id: str, identity: Principal = identity_dependency
    ) -> dict[str, str]:
        require_scope(identity, "conversations:write")
        conversation = await dependencies.conversations.get(conversation_id, identity.tenant_id)
        if conversation.user_id != identity.user_id:
            raise NotFoundError(f"conversation {conversation_id!r} not found")
        await dependencies.conversations.cancel(conversation_id, identity.tenant_id)
        return {"status": "cancellation_requested"}

    @app.post(
        "/v1/conversations/{conversation_id}/turns/{turn_id}/approval",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def decide_conversation_turn(
        conversation_id: str,
        turn_id: str,
        body: ConversationApprovalDecision,
        request: Request,
        identity: Principal = identity_dependency,
    ) -> dict[str, Any]:
        # The decider is the authenticated user, never a field of the body.
        require_scope(identity, "conversations:write")
        message = await dependencies.conversations.respond(
            conversation_id,
            turn_id,
            identity.tenant_id,
            identity.user_id,
            body,
            **caller_credentials(request),
        )
        return {"turn_id": turn_id, "assistant_message_id": message.id, "status": message.status}

    @app.post(
        "/v1/conversations/{conversation_id}/turns/{turn_id}/input",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def answer_conversation_turn(
        conversation_id: str,
        turn_id: str,
        body: ConversationInputAnswer,
        request: Request,
        identity: Principal = identity_dependency,
    ) -> dict[str, Any]:
        require_scope(identity, "conversations:write")
        message = await dependencies.conversations.respond(
            conversation_id,
            turn_id,
            identity.tenant_id,
            identity.user_id,
            body,
            **caller_credentials(request),
        )
        return {"turn_id": turn_id, "assistant_message_id": message.id, "status": message.status}

    @app.post(
        "/v1/conversations/{conversation_id}/turns/{turn_id}/credentials",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def renew_conversation_turn_credentials(
        conversation_id: str,
        turn_id: str,
        request: Request,
        identity: Principal = identity_dependency,
    ) -> dict[str, Any]:
        # A turn whose run was recovered without the caller's credentials: they come from the
        # headers api.credential_headers lists; with none, the service answers 409.
        require_scope(identity, "conversations:write")
        message = await dependencies.conversations.respond(
            conversation_id,
            turn_id,
            identity.tenant_id,
            identity.user_id,
            ConversationCredentialsRenewal(),
            **caller_credentials(request),
        )
        return {"turn_id": turn_id, "assistant_message_id": message.id, "status": message.status}

    @app.post(
        "/v1/conversations/{conversation_id}/turns/{turn_id}/steer",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def steer_conversation_turn(
        conversation_id: str,
        turn_id: str,
        body: SteerBody,
        identity: Principal = identity_dependency,
    ) -> dict[str, Any]:
        # Steering puts text in front of the model as the user: only the owner may.
        require_scope(identity, "conversations:write")
        try:
            message = await dependencies.conversations.steer(
                conversation_id, turn_id, identity.tenant_id, identity.user_id, body.text
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"turn_id": turn_id, "steer_id": message.id, "status": "queued"}

    @app.put("/v1/conversations/{conversation_id}/messages/{message_id}/feedback")
    async def put_conversation_feedback(
        conversation_id: str,
        message_id: str,
        body: ConversationFeedbackBody,
        identity: Principal = identity_dependency,
    ) -> dict[str, Any]:
        require_scope(identity, "feedback:write")
        feedback = await dependencies.conversations.record_feedback(
            conversation_id,
            message_id,
            identity.tenant_id,
            identity.user_id,
            body.rating,
            category=body.category,
            comment=body.comment,
            tags=body.tags,
        )
        return feedback.model_dump(mode="json")

    @app.get("/v1/conversations/{conversation_id}/messages/{message_id}/feedback")
    async def get_conversation_feedback(
        conversation_id: str,
        message_id: str,
        identity: Principal = identity_dependency,
    ) -> dict[str, Any] | None:
        require_scope(identity, "feedback:read")
        feedback = await dependencies.conversations.feedback(
            conversation_id, message_id, identity.tenant_id, identity.user_id
        )
        return feedback.model_dump(mode="json") if feedback else None

    @app.get("/v1/conversations/{conversation_id}/events")
    async def stream_conversation_events(
        conversation_id: str,
        request: Request,
        identity: Principal = identity_dependency,
    ) -> StreamingResponse:
        require_scope(identity, "conversations:read")
        conversation = await dependencies.conversations.get(conversation_id, identity.tenant_id)
        if conversation.user_id != identity.user_id:
            raise NotFoundError(f"conversation {conversation_id!r} not found")
        last_id = int(request.headers.get("last-event-id", "0") or 0)

        async def generate_conversation_events() -> AsyncIterator[bytes]:
            delivered = last_id
            iterator = dependencies.conversations.events.subscribe(
                conversation_id, after=last_id
            ).__aiter__()
            pending: asyncio.Future[ConversationEvent] = asyncio.ensure_future(iterator.__anext__())
            try:
                while True:
                    done, _ = await asyncio.wait((pending,), timeout=15)
                    if not done:
                        yield b": keepalive\n\n"
                        continue
                    try:
                        event = pending.result()
                    except StopAsyncIteration:
                        return
                    pending = asyncio.ensure_future(iterator.__anext__())
                    if event.sequence <= delivered:
                        continue
                    delivered = event.sequence
                    yield _conversation_sse(event)
            finally:
                pending.cancel()

        return StreamingResponse(
            generate_conversation_events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/agents")
    async def list_agents(identity: Principal = identity_dependency) -> list[dict[str, Any]]:
        require_scope(identity, "agents:read")
        return [item.model_dump(mode="json") for item in dependencies.agents.list()]

    @app.post("/v1/agents", status_code=201)
    async def register_agent(
        definition: AgentDefinition, identity: Principal = identity_dependency
    ) -> dict[str, str]:
        require_scope(identity, "agents:write")
        dependencies.agents.register(definition)
        return {"key": definition.key}

    @app.get("/v1/semantic-layers")
    async def list_semantic_layers(
        identity: Principal = identity_dependency,
    ) -> list[dict[str, Any]]:
        require_scope(identity, "agents:read")
        return [
            {
                "name": layer.definition.name,
                "version": layer.definition.version,
                "description": layer.definition.description,
                "digest": layer.digest,
                "model_count": len(layer.definition.models),
            }
            for layer in (
                dependencies.semantics.get(name) for name in dependencies.semantics.list()
            )
        ]

    @app.get("/v1/semantic-layers/{name}")
    async def read_semantic_layer(
        name: str, identity: Principal = identity_dependency
    ) -> dict[str, Any]:
        require_scope(identity, "agents:read")
        layer = dependencies.semantics.get(name)
        return {
            "definition": layer.definition.model_dump(mode="json"),
            "digest": layer.digest,
        }

    @app.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def readiness(response: Response) -> dict[str, Any]:
        # 503 only when a required dependency is down or the service drains; see
        # orchestration/readiness.py and runtime.readiness.
        ready, body = await dependencies.readiness_report()
        if not ready:
            response.status_code = 503
        return body

    ui_path = resolved.api.agent_ui_path.rstrip("/")
    if not ui_path.startswith("/"):
        ui_path = f"/{ui_path}"

    if resolved.api.agent_ui_enabled:
        from algen_agent_runtime.ui.generator import generate_agent_ui_html
        from algen_agent_runtime.ui.spec import extract_agent_manifest_spec

        @app.get(ui_path, response_class=HTMLResponse, include_in_schema=False)
        @app.get(f"{ui_path}/", response_class=HTMLResponse, include_in_schema=False)
        async def agent_ui_page(request: Request) -> HTMLResponse:
            base_url = str(request.base_url).rstrip("/")
            content = generate_agent_ui_html(
                settings=resolved,
                container=dependencies,
                live_server=True,
                api_base_url=base_url,
            )
            return HTMLResponse(content=content)

        @app.get(f"{ui_path}/spec", response_class=JSONResponse, include_in_schema=False)
        async def agent_ui_spec(request: Request) -> JSONResponse:
            base_url = str(request.base_url).rstrip("/")
            spec_data = extract_agent_manifest_spec(
                settings=resolved,
                container=dependencies,
                live_server=True,
                api_base_url=base_url,
            )
            return JSONResponse(content=spec_data)

        if ui_path != "/ui":

            @app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
            @app.get("/ui/", response_class=HTMLResponse, include_in_schema=False)
            async def agent_ui_alias(request: Request) -> HTMLResponse:
                return await agent_ui_page(request)
    else:

        @app.get(ui_path, response_class=JSONResponse, include_in_schema=False)
        @app.get(f"{ui_path}/", response_class=JSONResponse, include_in_schema=False)
        async def agent_ui_disabled() -> JSONResponse:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": (
                        "Agent UI is disabled. Set api.agent_ui_enabled=true in agent.yaml "
                        "or start the server with --ui flag."
                    )
                },
            )

    return app


def main() -> None:
    config_value = os.getenv("ALGEN_AGENT_RUNTIME_CONFIG", "")
    config_files = tuple(filter(None, config_value.split(os.pathsep)))
    settings = (
        load_settings(tuple(Path(item) for item in config_files)) if config_files else AppSettings()
    )
    uvicorn.run(
        create_app(settings),
        host=settings.api.host,
        port=settings.api.port,
        # The trusted-proxy identity hook checks the direct peer; never let X-Forwarded-For
        # rewrite it.
        proxy_headers=not settings.api.trusted_proxy_identity.enabled,
    )


app = create_app()

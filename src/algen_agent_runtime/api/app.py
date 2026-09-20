from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from algen_agent_runtime import __version__
from algen_agent_runtime.api.dependencies import Principal, principal_dependency, require_scope
from algen_agent_runtime.config.settings import AppSettings, load_settings
from algen_agent_runtime.conversations.contracts import ConversationEvent, ConversationStatus
from algen_agent_runtime.conversations.feedback import FeedbackRating
from algen_agent_runtime.events.contracts import RunEvent
from algen_agent_runtime.exceptions.errors import (
    AlgenAgentRuntimeError,
    ConflictError,
    NotFoundError,
)
from algen_agent_runtime.observability.setup import configure_logging, configure_telemetry
from algen_agent_runtime.orchestration.container import Container, build_container
from algen_agent_runtime.types.contracts import AgentDefinition, RequestOverrides, RunRequest


class CreateRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str
    agent_version: str | None = None
    input: str = Field(min_length=1)
    session_id: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    stream: bool = False
    metadata: dict[str, str] = Field(default_factory=dict)
    overrides: RequestOverrides = Field(default_factory=RequestOverrides)


class ClarificationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clarification: str = Field(min_length=1)


class ApprovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str
    modified_parameters: dict[str, Any] | None = None


class CreateConversationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str = Field(min_length=1)
    handler: str = "runtime"
    title: str = Field(default="New conversation", min_length=1, max_length=160)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateConversationMessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=100_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


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


def _conversation_sse(event: ConversationEvent) -> bytes:
    return (
        f"id: {event.sequence}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"
    ).encode()


def create_app(
    settings: AppSettings | None = None,
    container: Container | None = None,
) -> FastAPI:
    resolved = settings or AppSettings()
    dependencies = container or build_container(resolved)
    authenticate = principal_dependency(resolved.api)
    identity_dependency = Depends(authenticate)
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        dependencies.observability.start()
        configure_telemetry(resolved.telemetry)
        app.state.container = dependencies
        try:
            await dependencies.astart()
            yield
        finally:
            await dependencies.aclose()

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

    @app.middleware("http")
    async def payload_limit(request: Request, call_next: Any) -> Response:
        length = int(request.headers.get("content-length", "0") or 0)
        if length > resolved.security.max_request_bytes:
            return JSONResponse(status_code=413, content={"detail": "request payload too large"})
        return cast(Response, await call_next(request))

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict(request: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(AlgenAgentRuntimeError)
    async def core_error(request: Request, exc: AlgenAgentRuntimeError) -> JSONResponse:
        return JSONResponse(
            status_code=400, content={"detail": str(exc), "kind": exc.error_kind.value}
        )

    @app.post("/v1/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(
        body: CreateRunBody, identity: Principal = identity_dependency
    ) -> dict[str, Any]:
        require_scope(identity, "runs:write")
        state = await dependencies.runtime.start(
            RunRequest(**body.model_dump(), tenant_id=identity.tenant_id, user_id=identity.user_id)
        )
        return {"run_id": state.id, "session_id": state.session_id, "status": state.status}

    @app.get("/v1/runs/{run_id}")
    async def read_run(run_id: str, identity: Principal = identity_dependency) -> dict[str, Any]:
        require_scope(identity, "runs:read")
        state = await dependencies.runtime.status(run_id, identity.tenant_id)
        return state.model_dump(mode="json", exclude={"messages", "pending_tool_calls"})

    @app.delete("/v1/runs/{run_id}")
    async def cancel_run(run_id: str, identity: Principal = identity_dependency) -> dict[str, Any]:
        require_scope(identity, "runs:write")
        state = await dependencies.runtime.cancel(run_id, identity.tenant_id)
        return {"run_id": state.id, "status": state.status}

    @app.post("/v1/runs/{run_id}/clarification", status_code=202)
    async def clarify_run(
        run_id: str, body: ClarificationBody, identity: Principal = identity_dependency
    ) -> dict[str, Any]:
        require_scope(identity, "runs:write")
        state = await dependencies.runtime.resume(run_id, identity.tenant_id, body.model_dump())
        return {"run_id": state.id, "status": state.status}

    @app.post("/v1/runs/{run_id}/approval", status_code=202)
    async def approve_run(
        run_id: str, body: ApprovalBody, identity: Principal = identity_dependency
    ) -> dict[str, Any]:
        require_scope(identity, "runs:write")
        state = await dependencies.runtime.resume(run_id, identity.tenant_id, body.model_dump())
        return {"run_id": state.id, "status": state.status}

    @app.get("/v1/runs/{run_id}/events")
    async def stream_events(
        run_id: str,
        request: Request,
        identity: Principal = identity_dependency,
    ) -> StreamingResponse:
        require_scope(identity, "runs:read")
        await dependencies.runtime.status(run_id, identity.tenant_id)
        last_id = int(request.headers.get("last-event-id", "0") or 0)

        async def generate() -> AsyncIterator[bytes]:
            delivered = last_id
            async for event in dependencies.events.subscribe(run_id, after=last_id):
                if event.sequence <= delivered:
                    continue
                delivered = event.sequence
                yield _sse(event)
                if event.type in {"run.completed", "run.failed", "run.cancelled"}:
                    return

        return StreamingResponse(
            generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/v1/artifacts/{artifact_id}")
    async def read_artifact(
        artifact_id: str, identity: Principal = identity_dependency
    ) -> Response:
        require_scope(identity, "runs:read")
        artifact = await dependencies.artifacts.get(artifact_id, identity.tenant_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        return Response(
            artifact.data,
            media_type=artifact.media_type,
            headers={"Content-Disposition": f'attachment; filename="{artifact.name}"'},
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
        identity: Principal = identity_dependency,
    ) -> dict[str, Any]:
        require_scope(identity, "conversations:write")
        user_message, assistant_message = await dependencies.conversations.submit(
            conversation_id,
            identity.tenant_id,
            identity.user_id,
            body.text,
            body.metadata,
        )
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
        providers = {}
        for provider in dependencies.runtime.router.providers():
            providers[provider.provider_id] = await provider.health()
        stores = await dependencies.resource_health()
        ready = all((*providers.values(), *stores.values()))
        if not ready:
            response.status_code = 503
        return {
            "status": "ready" if ready else "degraded",
            "providers": providers,
            "stores": stores,
        }

    return app


def main() -> None:
    config_value = os.getenv("ALGEN_AGENT_RUNTIME_CONFIG", "")
    config_files = tuple(filter(None, config_value.split(os.pathsep)))
    settings = (
        load_settings(tuple(Path(item) for item in config_files)) if config_files else AppSettings()
    )
    uvicorn.run(create_app(settings), host=settings.api.host, port=settings.api.port)


app = create_app()

import asyncio
from collections.abc import AsyncIterator

import httpx
from conftest import make_runtime

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.conversations.service import (
    ConversationHandlerRegistry,
    ConversationService,
    RuntimeConversationHandler,
)
from algen_agent_runtime.conversations.stores import (
    InMemoryConversationEventBus,
    InMemoryConversationStore,
)
from algen_agent_runtime.orchestration.container import Container
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.semantics import (
    Aggregation,
    DimensionDefinition,
    MetricDefinition,
    SemanticLayer,
    SemanticLayerDefinition,
    SemanticModelDefinition,
    SemanticType,
)
from algen_agent_runtime.types.contracts import RunStatus


def make_container() -> Container:
    runtime = make_runtime()
    handlers = ConversationHandlerRegistry()
    handlers.register(RuntimeConversationHandler(runtime))
    conversations = ConversationService(
        InMemoryConversationStore(), InMemoryConversationEventBus(), handlers
    )
    return Container(
        runtime=runtime,
        agents=runtime.agents,
        tools=runtime.tools,
        artifacts=InMemoryArtifactStore(),
        events=runtime.events,
        conversations=conversations,
    )


async def test_rest_create_and_read_run() -> None:
    container = make_container()
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    headers = {
        "x-tenant-id": "tenant",
        "x-user-id": "user",
        "x-scopes": "runs:read runs:write",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/runs", headers=headers, json={"agent": "test-agent", "input": "hello"}
        )
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        for _ in range(100):
            response = await client.get(f"/v1/runs/{run_id}", headers=headers)
            if response.json()["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(0.001)
        assert response.json()["status"] == RunStatus.COMPLETED
        events = await client.get(f"/v1/runs/{run_id}/events", headers=headers)
        assert events.status_code == 200
        assert "event: run.completed" in events.text
        assert "event: model.completed" in events.text


async def test_api_enforces_tenant_identity() -> None:
    container = make_container()
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/runs", json={"agent": "test-agent", "input": "x"})
    assert response.status_code == 401


async def test_artifact_lifecycle_api_is_tenant_scoped() -> None:
    container = make_container()
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    headers = {
        "x-tenant-id": "tenant",
        "x-user-id": "user",
        "x-scopes": "runs:read runs:write artifacts:scan",
        "x-artifact-metadata": '{"purpose":"candidate_resume"}',
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/artifacts?name=candidate.pdf&media_type=application%2Fpdf",
            headers=headers,
            content=b"resume",
        )
        assert created.status_code == 201
        artifact = created.json()
        assert artifact["status"] == "pending_scan"
        assert artifact["size_bytes"] == 6
        assert artifact["metadata"] == {"purpose": "candidate_resume"}

        pending = await client.get(f"/v1/artifacts/{artifact['id']}", headers=headers)
        assert pending.status_code == 409

        listed = await client.get("/v1/artifacts", headers=headers)
        assert [item["id"] for item in listed.json()] == [artifact["id"]]

        run_writer = {**headers, "x-scopes": "runs:read runs:write"}
        forbidden = await client.patch(
            f"/v1/artifacts/{artifact['id']}/status",
            headers=run_writer,
            json={"status": "available"},
        )
        assert forbidden.status_code == 403

        updated = await client.patch(
            f"/v1/artifacts/{artifact['id']}/status",
            headers=headers,
            json={"status": "available"},
        )
        assert updated.json()["status"] == "available"
        audit_events = await container.runtime.audits.list("tenant", artifact["id"])
        assert audit_events[-1].action == "artifact.status.override"

        downloaded = await client.get(f"/v1/artifacts/{artifact['id']}", headers=headers)
        assert downloaded.content == b"resume"
        assert downloaded.headers["x-artifact-status"] == "available"
        assert len(downloaded.headers["x-artifact-sha256"]) == 64

        other_tenant = {**headers, "x-tenant-id": "other"}
        missing = await client.get(f"/v1/artifacts/{artifact['id']}", headers=other_tenant)
        assert missing.status_code == 404

        deleted = await client.delete(f"/v1/artifacts/{artifact['id']}", headers=headers)
        assert deleted.status_code == 204


async def test_cors_preflight_allows_dashboard_identity_headers() -> None:
    settings = AppSettings.model_validate(
        {
            "api": {
                "cors_allowed_origins": ["http://localhost:5173"],
                "cors_allow_credentials": True,
            }
        }
    )
    transport = httpx.ASGITransport(app=create_app(settings, make_container()))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.options(
            "/v1/conversations",
            headers={
                "origin": "http://localhost:5173",
                "access-control-request-method": "POST",
                "access-control-request-headers": (
                    "content-type,x-tenant-id,x-user-id,x-scopes,x-artifact-metadata"
                ),
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "x-artifact-metadata" in response.headers["access-control-allow-headers"].lower()
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "x-tenant-id" in allowed
    assert "x-user-id" in allowed


async def test_conversation_api_supports_dashboard_message_flow() -> None:
    container = make_container()
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    headers = {
        "x-tenant-id": "tenant",
        "x-user-id": "user",
        "x-scopes": "conversations:read conversations:write",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/conversations",
            headers=headers,
            json={"agent": "test-agent"},
        )
        assert created.status_code == 201
        conversation_id = created.json()["id"]
        renamed = await client.patch(
            f"/v1/conversations/{conversation_id}",
            headers=headers,
            json={"title": "Revenue analysis"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "Revenue analysis"
        accepted = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={"text": "hello"},
        )
        assert accepted.status_code == 202
        for _ in range(100):
            messages = await client.get(
                f"/v1/conversations/{conversation_id}/messages", headers=headers
            )
            if messages.json()[-1]["status"] == "completed":
                break
            await asyncio.sleep(0.001)
        assert messages.json()[-1]["content"] == [{"type": "text", "text": "answer"}]
        assert messages.json()[-1]["run_ids"]


async def test_conversation_api_records_and_updates_feedback() -> None:
    container = make_container()
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    headers = {
        "x-tenant-id": "tenant",
        "x-user-id": "user",
        "x-scopes": "conversations:read conversations:write feedback:read feedback:write",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/conversations", headers=headers, json={"agent": "test-agent"}
        )
        conversation_id = created.json()["id"]
        accepted = await client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={"text": "hello"},
        )
        message_id = accepted.json()["assistant_message_id"]
        for _ in range(100):
            messages = await client.get(
                f"/v1/conversations/{conversation_id}/messages", headers=headers
            )
            if messages.json()[-1]["status"] == "completed":
                break
            await asyncio.sleep(0.001)

        saved = await client.put(
            f"/v1/conversations/{conversation_id}/messages/{message_id}/feedback",
            headers=headers,
            json={"rating": "up", "tags": ["helpful"]},
        )
        updated = await client.put(
            f"/v1/conversations/{conversation_id}/messages/{message_id}/feedback",
            headers=headers,
            json={"rating": "down", "category": "incorrect"},
        )
        fetched = await client.get(
            f"/v1/conversations/{conversation_id}/messages/{message_id}/feedback",
            headers=headers,
        )

    assert saved.status_code == 200
    assert updated.json()["id"] == saved.json()["id"]
    assert fetched.json()["rating"] == "down"


async def test_api_exposes_registered_semantic_layers() -> None:
    container = make_container()
    container.semantics.register(
        SemanticLayer(
            SemanticLayerDefinition(
                name="commercial",
                version="1.0.0",
                description="Commercial definitions",
                models=(
                    SemanticModelDefinition(
                        name="sales",
                        description="Sales facts",
                        table="sales",
                        primary_key=("id",),
                        dimensions=(
                            DimensionDefinition(
                                name="route",
                                label="Route",
                                description="Market",
                                expression="route",
                                type=SemanticType.STRING,
                            ),
                        ),
                        metrics=(
                            MetricDefinition(
                                name="revenue",
                                label="Revenue",
                                description="Recognized revenue",
                                expression="amount",
                                aggregation=Aggregation.SUM,
                                allowed_dimensions=("route",),
                            ),
                        ),
                    ),
                ),
            )
        )
    )
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    headers = {
        "x-tenant-id": "tenant",
        "x-user-id": "user",
        "x-scopes": "agents:read",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/v1/semantic-layers", headers=headers)
        detail = await client.get("/v1/semantic-layers/commercial", headers=headers)

    assert listed.status_code == 200
    assert listed.json()[0]["name"] == "commercial"
    assert listed.json()[0]["model_count"] == 1
    assert detail.status_code == 200
    assert detail.json()["definition"]["models"][0]["metrics"][0]["name"] == "revenue"


async def test_api_rejects_oversized_payload_stream() -> None:
    settings = AppSettings()
    # Configure 1024 byte limit for test
    from algen_agent_runtime.config.settings import SecuritySettings

    settings = settings.model_copy(
        update={"security": SecuritySettings(max_request_bytes=1024, max_artifact_bytes=2048)}
    )
    container = make_container()
    app = create_app(settings, container)
    transport = httpx.ASGITransport(app=app)
    headers = {
        "x-tenant-id": "tenant",
        "x-user-id": "user",
        "x-scopes": "runs:write",
    }

    # Stream generator yielding chunks that exceed 1024 bytes
    async def chunk_gen() -> AsyncIterator[bytes]:
        for _ in range(5):
            yield b'{"agent": "test-agent", "input": "' + (b"x" * 300) + b'"}'

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Chunked stream without Content-Length
        resp = await client.post(
            "/v1/runs",
            headers=headers,
            content=chunk_gen(),
        )
        assert resp.status_code == 413
        data = resp.json()
        assert data["code"] == "REQUEST_PAYLOAD_TOO_LARGE"
        assert data["limit_bytes"] == 1024

        # 2. Declared Content-Length exceeding limit
        resp_declared = await client.post(
            "/v1/runs",
            headers={**headers, "content-length": "2000"},
            content=b"x" * 2000,
        )
        assert resp_declared.status_code == 413
        assert resp_declared.json()["code"] == "REQUEST_PAYLOAD_TOO_LARGE"

        # 3. Invalid Content-Length
        resp_invalid = await client.post(
            "/v1/runs",
            headers={**headers, "content-length": "-1"},
            content=b"x",
        )
        assert resp_invalid.status_code == 400
        assert resp_invalid.json()["code"] == "INVALID_CONTENT_LENGTH"

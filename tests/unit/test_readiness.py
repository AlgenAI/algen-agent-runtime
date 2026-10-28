"""/health/ready: declared dependencies, required versus optional, reasons, and drain."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from pydantic import ValidationError

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.config.settings import AppSettings, ReadinessSettings
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.orchestration.check import check_project
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.orchestration.readiness import content_minimal_reason
from algen_agent_runtime.persistence.postgres import PostgresDatabase


class Resource:
    def __init__(self, healthy: bool = True, delay: float = 0) -> None:
        self.healthy, self.delay = healthy, delay

    async def health(self) -> bool:
        await asyncio.sleep(self.delay)
        return self.healthy


class FlakyResource(Resource):
    async def health(self) -> bool:
        raise ConnectionError("cannot reach postgresql://app:hunter2@db.internal/app")


class Retriever:
    def __init__(self, healthy: bool | None) -> None:
        if healthy is not None:
            self.health = lambda: healthy


class DownProvider(MockModelProvider):
    provider_id = "down"

    async def health(self) -> bool:
        return False


def _settings(**runtime: Any) -> AppSettings:
    return AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
            "runtime": {"readiness": runtime} if runtime else {},
        }
    )


async def _ready(container: Any, settings: AppSettings | None = None) -> httpx.Response:
    app = create_app(settings or _settings(), container)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.get("/health/ready")


async def test_a_default_container_is_ready_and_reports_every_store() -> None:
    container = build_container(_settings())
    response = await _ready(container)
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ready" and body["ready"] is True
    # Earlier keys stay.
    assert body["providers"] == {"mock": True}
    assert isinstance(body["stores"], dict)
    dependencies = body["dependencies"]
    for kind in (
        "run",
        "event",
        "approval",
        "audit",
        "artifact",
        "workflow",
        "tool_execution",
        "conversation",
    ):
        assert dependencies[f"store.{kind}"] == {
            "kind": "store",
            "required": True,
            "backend": "memory",
            "status": "up",
        }
    assert dependencies["provider.mock"]["status"] == "up"
    assert body["degraded_dependencies"] == {} and body["failing"] == []


async def test_an_optional_mcp_failure_degrades_without_failing_and_hides_credentials() -> None:
    container = build_container(_settings())
    container.degraded_dependencies["mcp.crm"] = (
        "connect to https://svc:s3cret@crm.example.com/mcp failed: token=abc123 rejected"
    )
    response = await _ready(container)
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "degraded" and body["ready"] is True
    reason = body["degraded_dependencies"]["mcp.crm"]
    assert body["dependencies"]["mcp.crm"] == {
        "kind": "mcp",
        "required": False,
        "status": "down",
        "reason": reason,
    }
    for secret in ("s3cret", "abc123", "crm.example.com", "https://"):
        assert secret not in response.text


async def test_a_required_resource_that_is_down_fails_readiness() -> None:
    container = dataclasses.replace(build_container(_settings()), resources=(Resource(False),))
    response = await _ready(container)
    body = response.json()

    assert response.status_code == 503
    assert body["status"] == "degraded" and body["ready"] is False
    assert body["failing"] == ["resource.Resource"]
    assert body["dependencies"]["resource.Resource"]["reason"] == "probe failed"
    assert body["stores"] == {"Resource": False}


async def test_an_operator_can_make_a_resource_optional() -> None:
    settings = _settings(optional=["resource.Resource"])
    container = dataclasses.replace(build_container(settings), resources=(Resource(False),))
    response = await _ready(container, settings)

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


async def test_a_slow_or_failing_probe_is_down_with_a_short_reason() -> None:
    settings = _settings(probe_timeout_seconds=0.05)
    container = dataclasses.replace(
        build_container(settings), resources=(Resource(delay=5), FlakyResource())
    )
    response = await asyncio.wait_for(_ready(container, settings), timeout=2)
    dependencies = response.json()["dependencies"]

    assert dependencies["resource.Resource"]["reason"] == "probe timed out"
    assert dependencies["resource.FlakyResource"]["reason"] == "ConnectionError"
    assert "hunter2" not in response.text
    assert response.status_code == 503


async def test_model_provider_rule_any_all_and_none() -> None:
    for rule, expected in (("any", 200), ("all", 503), ("none", 200)):
        settings = _settings(model_providers=rule)
        container = build_container(settings)
        container.runtime.router.register_provider(DownProvider())
        response = await _ready(container, settings)
        assert response.status_code == expected, rule
        assert response.json()["providers"] == {"down": False, "mock": True}

    container = build_container(AppSettings())
    container.runtime.router.register_provider(DownProvider())
    response = await _ready(container, AppSettings())
    assert response.status_code == 503
    assert response.json()["failing"] == ["provider.*"]


async def test_named_dependencies_can_be_required() -> None:
    settings = _settings(required=["retriever.docs", "mcp.missing"])
    container = build_container(settings)
    container.retrievers.register("docs", Retriever(False))  # type: ignore[arg-type]
    container.retrievers.register("faq", Retriever(None))  # type: ignore[arg-type]
    response = await _ready(container, settings)
    body = response.json()

    assert response.status_code == 503
    assert body["failing"] == ["mcp.missing", "retriever.docs"]
    assert body["dependencies"]["mcp.missing"]["reason"] == "not registered"
    # A retriever without health() is reported but never fails readiness.
    assert body["dependencies"]["retriever.faq"]["status"] == "unknown"
    assert body["dependencies"]["retriever.faq"]["required"] is False


async def test_durable_stores_are_probed_through_their_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_POSTGRES_DSN", "postgresql://runtime:test@localhost/runtime")

    async def down(self: PostgresDatabase) -> bool:
        return False

    monkeypatch.setattr(PostgresDatabase, "health", down)
    settings = AppSettings.model_validate(
        {
            "storage": {
                "run_store": "postgres",
                "workflow_store": "postgres",
                "postgres_dsn": "env://TEST_POSTGRES_DSN",
            },
            "telemetry": {"otlp_endpoint": "https://otel.example.com:4318"},
        }
    )
    container = build_container(settings)
    response = await _ready(container, settings)
    body = response.json()

    assert response.status_code == 503
    assert body["failing"] == ["store.run", "store.workflow"]
    assert body["dependencies"]["store.run"]["backend"] == "postgres"
    assert body["dependencies"]["store.event"]["status"] == "up"
    # The database is reported once, as the stores it backs, and still under the earlier key.
    assert not any(name.startswith("resource.") for name in body["dependencies"])
    assert body["stores"]["PostgresDatabase"] is False
    assert body["dependencies"]["telemetry.otlp"]["status"] == "unknown"
    assert "otel.example.com" not in response.text
    await container.aclose()

    relaxed = settings.model_copy(
        update={
            "runtime": settings.runtime.model_copy(
                update={"readiness": ReadinessSettings(require_durable_stores=False)}
            )
        }
    )
    container = build_container(relaxed)
    response = await _ready(container, relaxed)
    assert response.status_code == 200 and response.json()["status"] == "degraded"
    await container.aclose()


async def test_readiness_is_false_while_draining() -> None:
    container = build_container(_settings())
    container.lifecycle.phase = "draining"
    response = await _ready(container)

    assert response.status_code == 503
    assert response.json()["status"] == "draining"
    assert response.json()["lifecycle"] == "draining"


async def test_container_phases_follow_start_and_close() -> None:
    container = build_container(_settings())
    assert container.lifecycle.phase == "created"
    await container.astart()
    assert container.lifecycle.phase == "ready"
    await container.aclose()
    assert container.lifecycle.phase == "stopped"
    ready, body = await container.readiness_report()
    assert not ready and body["status"] == "draining"


def test_readiness_settings_reject_unknown_kinds_and_contradictions() -> None:
    with pytest.raises(ValidationError):
        ReadinessSettings(required=("database",))
    with pytest.raises(ValidationError):
        ReadinessSettings(required=("queue.jobs",))
    with pytest.raises(ValidationError):
        ReadinessSettings(required=("mcp.crm",), optional=("mcp.crm",))
    with pytest.raises(ValidationError):
        ReadinessSettings(probe_timeout_seconds=0)
    assert ReadinessSettings().model_providers == "any"


CHECK_YAML = """
providers:
  local: {type: mock, default_model: m}
agents:
  - name: operator
    version: 1.0.0
    description: d
    system_instructions: x
    default_model: {name: m, provider: local}
runtime:
  recover_service_workflows: true
  readiness:
    model_providers: all
    required: [mcp.crm, retriever.docs]
    optional: [cache.redis]
    probe_timeout_seconds: 1.5
maintenance:
  enabled: true
  expire_paused_workflows: true
  clarification_timeout_seconds: 86400
"""


def test_check_accepts_the_readiness_and_lifecycle_settings(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(CHECK_YAML)
    assert check_project((path,)).ok
    path.write_text(CHECK_YAML.replace("mcp.crm", "database"))
    assert [issue.code for issue in check_project((path,)).errors] == ["CONFIGURATION_ERROR"]


def test_reasons_are_short_and_free_of_credentials() -> None:
    reason = content_minimal_reason(
        "Authorization: Bearer eyJhbGciOi failed for redis://:pw@cache:6379/0 " + "x" * 400
    )
    assert "eyJhbGciOi" not in reason and "pw@" not in reason
    assert len(reason) <= 160
    assert content_minimal_reason("") == "unavailable"

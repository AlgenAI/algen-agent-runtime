"""Regression tests for error text and logs that left the Runtime unmasked in 0.2.0b4 and the
first golden telemetry suite did not cover (0.2.0b5): API error bodies, readiness reasons,
work-queue rows, developer-facing conversation errors, steering held for a paused run, artifact
metadata, MCP servers that echo the credentials they were sent, conversation titles cut inside
an identifier, and the Runtime's loggers under a host's own structlog configuration.

The values below are synthetic test fixtures, not credentials; key-shaped ones are built at run
time so secret scanners never see a literal.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import structlog
from conftest import make_agent, make_runtime

from algen_agent_runtime.conversations.presentation import ConversationPresentation
from algen_agent_runtime.conversations.service import TITLE_CHARS, conversation_title
from algen_agent_runtime.exceptions.errors import ConflictError, ToolExecutionError
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.observability.safe_logging import (
    RedactingLogger,
    host_masks_logs,
    runtime_logger,
)
from algen_agent_runtime.orchestration.readiness import content_minimal_reason
from algen_agent_runtime.security import (
    redact_error_text,
    redact_held,
    redact_log_event,
)
from algen_agent_runtime.tools.ask_user import ASK_USER_PERMISSION, ASK_USER_TOOL
from algen_agent_runtime.types.contracts import RunRequest, RunStatus

KEY = "-".join(("sk", "live", "0123456789abcdefghij"))
EMAIL = "jane.doe@example.com"
LEAKY = f"upstream refused api_key={KEY} for {EMAIL}"
HELD = "".join(("opaque", "Held", "Token", "42"))


# --- the helpers -------------------------------------------------------------------------------


def test_redact_error_text_masks_credentials_and_personal_data_and_keeps_the_rest() -> None:
    text = redact_error_text(ConflictError(LEAKY))
    assert KEY not in text and EMAIL not in text
    assert text.startswith("upstream refused") and "[REDACTED_EMAIL]" in text
    assert redact_error_text("plain message") == "plain message"


def test_redact_held_masks_known_values_at_any_depth_longest_first() -> None:
    value = {"a": [f"x {HELD} y", (HELD[:6], HELD)], "b": {"c": f"{HELD}{HELD}"}, "n": 3}
    masked = redact_held(value, [HELD[:6], HELD, ""])
    assert HELD not in json.dumps(masked)
    assert masked["a"][0] == "x [REDACTED] y"
    assert masked["b"]["c"] == "[REDACTED][REDACTED]" and masked["n"] == 3
    assert redact_held("unchanged", []) == "unchanged"


def test_a_readiness_reason_masks_token_shapes_and_personal_data() -> None:
    reason = content_minimal_reason(f"discovery failed with {KEY} for {EMAIL}")
    assert KEY not in reason and EMAIL not in reason and reason.startswith("discovery failed")


# --- conversation titles -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "identifier", [EMAIL, "4242 4242 4242 4242", "phone 9876501234"], ids=["email", "card", "phone"]
)
def test_a_title_is_cut_before_an_identifier_that_does_not_fit(identifier: str) -> None:
    prefix = "x" * (TITLE_CHARS - 8)
    title = conversation_title(f"{prefix} {identifier} and more")
    assert len(title) <= TITLE_CHARS
    assert title == prefix or title.startswith(prefix)
    # Nothing of the identifier is left to be mistaken for something else.
    assert not any(part in title for part in identifier.split() if any(c.isdigit() for c in part))
    assert "@" not in title


def test_a_title_keeps_a_whole_identifier_and_masks_credentials() -> None:
    assert conversation_title(f"mail {EMAIL} please") == f"mail {EMAIL} please"
    title = conversation_title(f"use {KEY} " + "y" * 100)
    assert KEY not in title and title.startswith("use [REDACTED]") and len(title) <= TITLE_CHARS


# --- error bodies and records ------------------------------------------------------------------


async def test_api_error_bodies_mask_the_exception_text() -> None:
    from algen_agent_runtime.api.app import create_app
    from algen_agent_runtime.config.settings import AppSettings
    from algen_agent_runtime.orchestration.container import build_container

    settings = AppSettings.model_validate(
        {"providers": {"mock": {"type": "mock", "default_model": "deterministic"}}}
    )
    app = create_app(settings, build_container(settings))

    async def conflict() -> None:
        raise ConflictError(LEAKY)

    async def tool_error() -> None:
        raise ToolExecutionError(LEAKY)

    app.add_api_route("/test/conflict", conflict)
    app.add_api_route("/test/tool-error", tool_error)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path, code in (("/test/conflict", 409), ("/test/tool-error", 400)):
            response = await client.get(path)
            assert response.status_code == code
            assert KEY not in response.text and EMAIL not in response.text
            assert "upstream refused" in response.json()["detail"]


async def test_a_failed_work_item_stores_its_error_masked() -> None:
    from algen_agent_runtime.distributed import DistributedWorker, InMemoryWorkQueue, WorkItem

    queue = InMemoryWorkQueue()
    worker = DistributedWorker("worker-1", queue, lease_seconds=10)

    async def failing(item: WorkItem) -> dict[str, Any]:
        raise RuntimeError(LEAKY)

    worker.register("leaky", failing)
    await queue.enqueue(
        WorkItem(
            id="item-1",
            tenant_id="t",
            kind="leaky",
            deduplication_key="item-1",
            payload={},
            maximum_attempts=1,
        )
    )
    assert await worker.run_once() is True
    stored = queue._items["item-1"]
    assert stored.error is not None
    assert KEY not in stored.error and EMAIL not in stored.error
    assert stored.error.startswith("upstream refused")


def test_developer_facing_conversation_errors_are_masked_before_they_are_cut() -> None:
    presentation = ConversationPresentation(error_audience="developer")
    error = RuntimeError("x" * 480 + f" {EMAIL}")
    assert EMAIL[:10] not in presentation.error_message(error)
    assert KEY not in presentation.unexpected_error_message(RuntimeError(LEAKY))


# --- steering ----------------------------------------------------------------------------------


async def test_steering_held_for_a_paused_run_is_stored_masked() -> None:
    runtime = make_runtime(
        MockModelProvider([tool_call_response(ASK_USER_TOOL, {"question": "Which?"}), "done"]),
        make_agent(
            enabled_tools=frozenset({ASK_USER_TOOL}),
            tool_permissions=frozenset({ASK_USER_PERMISSION}),
        ),
    )
    paused = await runtime.run(
        RunRequest(agent="test-agent", input="q", tenant_id="t", user_id="u")
    )
    assert paused.status == RunStatus.AWAITING_CLARIFICATION
    message = await runtime.steer(paused.run_id, "t", LEAKY)
    assert KEY not in message.text and EMAIL not in message.text
    state = await runtime.status(paused.run_id, "t")
    assert KEY not in state.model_dump_json() and EMAIL not in state.model_dump_json()
    await runtime.cancel(paused.run_id, "t")
    result = await runtime.wait(paused.run_id, "t")
    assert [item.text for item in result.undelivered_steering] == [message.text]


async def test_steering_follows_an_agent_without_the_pii_guardrail() -> None:
    from algen_agent_runtime.types.contracts import GuardrailPolicy

    runtime = make_runtime(
        MockModelProvider([tool_call_response(ASK_USER_TOOL, {"question": "Which?"}), "done"]),
        make_agent(
            enabled_tools=frozenset({ASK_USER_TOOL}),
            tool_permissions=frozenset({ASK_USER_PERMISSION}),
            guardrail_policy=GuardrailPolicy(policies=("secrets",)),
        ),
    )
    paused = await runtime.run(
        RunRequest(agent="test-agent", input="q", tenant_id="t", user_id="u")
    )
    message = await runtime.steer(paused.run_id, "t", LEAKY)
    assert KEY not in message.text and EMAIL in message.text


# --- artifact metadata -------------------------------------------------------------------------


async def test_uploaded_artifact_metadata_has_credentials_masked() -> None:
    from algen_agent_runtime.api.app import create_app
    from algen_agent_runtime.config.settings import AppSettings
    from algen_agent_runtime.orchestration.container import build_container

    settings = AppSettings.model_validate(
        {"providers": {"mock": {"type": "mock", "default_model": "deterministic"}}}
    )
    container = build_container(settings)
    transport = httpx.ASGITransport(app=create_app(settings, container))
    headers = {"x-tenant-id": "t", "x-user-id": "u", "x-scopes": "artifacts:write"}
    metadata = {"label": f"for {EMAIL}", "api_key": KEY, "note": f"key {KEY}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/artifacts",
            params={"name": "notes.txt", "media_type": "text/plain"},
            headers={**headers, "x-artifact-metadata": json.dumps(metadata)},
            content=b"hello",
        )
    assert response.status_code == 201, response.text
    stored = response.json()["metadata"]
    assert stored["api_key"] == "[REDACTED]" and KEY not in stored["note"]
    assert stored["label"] == f"for {EMAIL}"  # personal data in labels is the user's own


# --- MCP servers that echo credentials ---------------------------------------------------------


class _EchoSession:
    def __init__(self, echo: str, *, fail: bool = False) -> None:
        self.echo = echo
        self.fail = fail

    async def initialize(self) -> None:
        pass

    async def list_tools(self, cursor: str | None = None) -> Any:
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="echo",
                    description="echo",
                    inputSchema={"type": "object", "properties": {}},
                    outputSchema=None,
                    annotations=None,
                )
            ]
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self.fail:
            raise RuntimeError(f"rejected {self.echo} for {EMAIL}")
        return SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(text=f"server saw {self.echo}, flag turned on")],
            structuredContent={"seen": self.echo},
        )


@pytest.mark.parametrize("fail", [False, True], ids=["result", "error"])
async def test_an_mcp_server_echoing_its_env_secret_has_it_masked(fail: bool) -> None:
    from algen_agent_runtime.mcp.client import MCPClientManager
    from algen_agent_runtime.mcp.contracts import MCPStreamableHttpServerConfig
    from algen_agent_runtime.tools.testing import fake_tool_context

    config = MCPStreamableHttpServerConfig(
        name="crm",
        url="https://mcp.example.com/mcp",
        auth_token_ref="env://CRM_TOKEN",
        secret_headers={"x-api-key": "env://CRM_KEY", "x-flag": "env://CRM_FLAG"},
    )
    secret_key = HELD[::-1]

    async def session_factory(config: Any, stack: Any) -> _EchoSession:
        return _EchoSession(f"{HELD} and {secret_key}", fail=fail)

    mcp = MCPClientManager(
        [config],
        # A short value is no credential: it is not masked (it would mangle every result).
        environment={"CRM_TOKEN": HELD, "CRM_KEY": secret_key, "CRM_FLAG": "on"},
        session_factory=session_factory,
    )
    [echo] = await mcp.discover_tools("crm")
    if fail:
        with pytest.raises(ToolExecutionError) as raised:
            await echo.handler({}, fake_tool_context())
        text = str(raised.value)
        assert raised.value.__cause__ is None and raised.value.__suppress_context__
        assert "RuntimeError" in text and EMAIL not in text
    else:
        text = json.dumps(await echo.handler({}, fake_tool_context()))
        assert "flag turned on" in text
    assert HELD not in text and secret_key not in text and "[REDACTED]" in text


# --- the Runtime's loggers ---------------------------------------------------------------------


@pytest.fixture
def default_structlog() -> Iterator[list[dict[str, Any]]]:
    """structlog's defaults with a processor in front that keeps every event as it arrives."""
    previous = structlog.get_config()
    structlog.reset_defaults()
    seen: list[dict[str, Any]] = []

    def keep(logger: Any, method: str, event: Any) -> Any:
        seen.append(dict(event))
        return event

    structlog.configure(processors=[keep, *structlog.get_config()["processors"]])
    try:
        yield seen
    finally:
        structlog.configure(**previous)


def test_runtime_loggers_mask_events_and_exceptions_under_default_structlog(
    default_structlog: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert not host_masks_logs()
    logger = runtime_logger("algen_agent_runtime.test")
    assert isinstance(logger, RedactingLogger)
    try:
        raise RuntimeError(LEAKY)
    except RuntimeError:
        logger.bind(api_key=KEY).exception("failed", detail=f"token {KEY}")
    logger.warning("warned", password=KEY)
    [failed, warned] = default_structlog
    assert "exc_info" not in failed, "the host's processors never get the exception object"
    assert failed["exception"].startswith("Traceback") and "RuntimeError" in failed["exception"]
    assert failed["api_key"] == "[REDACTED]" and warned["password"] == "[REDACTED]"
    printed = capsys.readouterr().out
    rendered = json.dumps(default_structlog, default=str) + printed
    assert KEY not in rendered and EMAIL not in rendered
    assert "failed" in printed and "warned" in printed


def test_runtime_loggers_pass_events_through_when_the_host_masks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous = structlog.get_config()
    seen: list[dict[str, Any]] = []

    def keep(logger: Any, method: str, event: Any) -> Any:
        seen.append(dict(event))
        return event

    structlog.configure(
        processors=[
            keep,
            structlog.processors.format_exc_info,
            redact_log_event,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
    )
    try:
        assert host_masks_logs()
        try:
            raise RuntimeError(LEAKY)
        except RuntimeError:
            runtime_logger("algen_agent_runtime.test").exception("failed")
    finally:
        structlog.configure(**previous)
    assert seen[0]["exc_info"] is True
    printed = capsys.readouterr().out
    assert "Traceback" in printed and KEY not in printed and EMAIL not in printed


def test_stdlib_loggers_log_a_masked_traceback(caplog: pytest.LogCaptureFixture) -> None:
    from algen_agent_runtime.auth.audit import AuthenticationAuditor

    caplog.set_level(logging.ERROR)

    class Failing:
        async def append(self, event: Any) -> None:
            raise RuntimeError(LEAKY)

    import asyncio

    auditor = AuthenticationAuditor(Failing())  # type: ignore[arg-type]
    asyncio.run(auditor._append(SimpleNamespace()))  # type: ignore[arg-type]
    [record] = caplog.records
    assert record.exc_info is None
    text = record.getMessage()
    assert "Traceback" in text and KEY not in text and EMAIL not in text

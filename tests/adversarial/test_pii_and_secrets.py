"""PII and secrets in tool output, model output and tool arguments: what the model, events,
audits, approvals, narrative, telemetry and logs may carry.

The values below are synthetic test fixtures, not credentials.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from adversarial_harness import (
    TENANT,
    assert_absent,
    build,
    calls,
    everything_recorded,
    request,
    seen_by_model,
    tool,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from algen_agent_runtime.exceptions.errors import ToolExecutionError
from algen_agent_runtime.observability.setup import configure_logging
from algen_agent_runtime.tools.contracts import SideEffect, ToolContext
from algen_agent_runtime.types.contracts import MemoryPolicy, RunStatus, ToolPolicy

pytestmark = pytest.mark.adversarial

# Built at run time so secret scanners never see a key-shaped literal.
API_KEY = "-".join(("sk", "live", "0123456789abcdef"))
BEARER = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlLXRlc3Q"
EMAIL = "jane.doe@example.com"
CARD = "4111 1111 1111 1111"
SENSITIVE = (API_KEY, BEARER, EMAIL, CARD)
LEAKY = (
    f"Customer {EMAIL} paid with card {CARD}. Use api_key={API_KEY} and send "
    f"Authorization: Bearer {BEARER}"
)


@pytest.fixture
def service_logging() -> Iterator[None]:
    """Logging as ``algen-agent-runtime serve`` configures it (JSON, with redaction)."""
    previous = structlog.get_config()
    configure_logging()
    yield
    structlog.configure(**previous)


def _spans(runtime: Any) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    tracing = TracerProvider()
    tracing.add_span_processor(SimpleSpanProcessor(exporter))
    runtime._tracer = tracing.get_tracer("adversarial")
    runtime.telemetry_include_content = True
    return exporter


def _span_text(exporter: InMemorySpanExporter) -> str:
    return "\n".join(
        f"{span.name} {dict(span.attributes or {})} {[event.attributes for event in span.events]}"
        for span in exporter.get_finished_spans()
    )


async def test_secrets_and_pii_in_a_tool_result_are_redacted_everywhere() -> None:
    """Threat: a tool returns customer data and credentials, and the model repeats them.
    Control: the after_tool, after_model and final_response policies redact them before the
    model, the answer, memory, events, audits, narrative and telemetry carry them."""
    crm, _ = tool("crm.lookup", result={"record": LEAKY})
    runtime, provider = build(
        [calls(("crm.lookup", {"id": 7})), f"Here it is: {LEAKY}"],
        [crm],
        memory_policy=MemoryPolicy(store_tool_payloads=True),
    )
    exporter = _spans(runtime)

    result = await runtime.run(request("Look up customer 7"))

    assert result.status == RunStatus.COMPLETED
    assert_absent(seen_by_model(provider), *SENSITIVE, where="the model's requests")
    assert_absent(result.output or "", *SENSITIVE, where="the answer")
    assert_absent(
        await everything_recorded(runtime, result.run_id), *SENSITIVE, where="the records"
    )
    state = await runtime.status(result.run_id, TENANT)
    remembered = await runtime.memory.get(TENANT, state.session_id, 100)
    assert remembered
    assert_absent(str(remembered), *SENSITIVE, where="conversation memory")
    assert_absent(_span_text(exporter), *SENSITIVE, where="telemetry")


async def test_secrets_in_tool_arguments_stay_out_of_the_approval_request() -> None:
    """Threat: the model puts credentials and personal data into a side-effecting call, so the
    approval request shown to a person (and stored, and announced) would carry them.
    Control: the approval's parameters, the pause payload and the approval event are redacted."""
    send, sent = tool("email.send", SideEffect.EXTERNAL)
    runtime, _ = build(
        [calls(("email.send", {"to": "ops", "body": LEAKY, "api_key": API_KEY}))], [send]
    )

    result = await runtime.run(request("Email ops the details"))

    assert result.status == RunStatus.AWAITING_APPROVAL
    assert sent.calls == []
    assert_absent(
        await everything_recorded(runtime, result.run_id),
        API_KEY,
        BEARER,
        where="the approval records",
    )


async def test_a_failing_tool_cannot_leak_a_secret_through_its_error(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str], service_logging: None
) -> None:
    """Threat: a tool fails with an exception whose message quotes a credential.
    Control: the run records and logs the error's type, never its text."""

    def fail(arguments: dict[str, Any], context: ToolContext) -> Any:
        raise RuntimeError(f"upstream refused api_key={API_KEY} Bearer {BEARER}")

    broken, _ = tool("billing.charge", result=fail)
    runtime, provider = build(
        [calls(("billing.charge", {})), "sorry"],
        [broken],
        tool_policy=ToolPolicy(on_error="return_to_model"),
    )
    caplog.set_level(logging.DEBUG)

    result = await runtime.run(request())

    assert result.status == RunStatus.COMPLETED
    leaked = (
        caplog.text
        + capsys.readouterr().out
        + seen_by_model(provider)
        + await everything_recorded(runtime, result.run_id)
    )
    assert_absent(leaked, API_KEY, BEARER, where="logs, the model or the records")


async def test_a_secret_the_user_pastes_is_redacted_before_the_model_reads_it() -> None:
    """Threat: a user pastes a credential into the request.
    Control: the input boundary redacts it before the model, memory and records see it."""
    runtime, provider = build(["noted"])

    result = await runtime.run(request(f"My token={API_KEY}; Authorization: Bearer {BEARER}"))

    assert result.status == RunStatus.COMPLETED
    assert_absent(seen_by_model(provider), API_KEY, BEARER, where="the model's requests")
    assert_absent(await everything_recorded(runtime, result.run_id), API_KEY, BEARER)


@pytest.mark.parametrize("on_error", ["return_to_model", "fail_run"])
async def test_a_tool_error_the_runtime_shows_cannot_carry_a_credential(
    on_error: str, capsys: pytest.CaptureFixture[str], service_logging: None
) -> None:
    """Threat: a tool raises the Runtime's own ToolExecutionError (whose text is shown to the
    model and kept as the run's error) with a credential in the message.
    Control: the shown text is redacted wherever it goes."""

    def fail(arguments: dict[str, Any], context: ToolContext) -> Any:
        raise ToolExecutionError(f"upstream said api_key={API_KEY}, Bearer {BEARER}")

    broken, _ = tool("billing.charge", result=fail)
    runtime, provider = build(
        [calls(("billing.charge", {})), "sorry"],
        [broken],
        tool_policy=ToolPolicy(on_error=on_error),  # type: ignore[arg-type]
    )

    result = await runtime.run(request())

    logs = capsys.readouterr().out
    if on_error == "fail_run":
        assert "agent_run_failed" in logs and "Traceback" in logs
    leaked = (
        (result.error or "")
        + logs
        + seen_by_model(provider)
        + await everything_recorded(runtime, result.run_id)
    )
    assert_absent(leaked, API_KEY, BEARER, where="the run's error, logs or records")

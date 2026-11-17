"""Logs of a host that does not use ``configure_logging``: structlog's default configuration
(``ConsoleRenderer``, which prints an exception's traceback) and a host chain that hands
exceptions to an error tracker. The Runtime's own loggers mask what they log before the host's
processors see it (0.2.0b5); a host that installs ``redact_log_event`` gets the events as they
were, exception objects included.

See golden_harness.py for the canaries.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from golden_harness import (
    HELD_CANARIES,
    PII_CANARIES,
    SECRET_CANARIES,
    assert_no_canary,
    leaky_text,
)
from golden_scenarios import (
    agent_definition,
    container,
    raising,
    request,
    settings,
    tool,
    tool_call,
)

from algen_agent_runtime.exceptions.errors import ToolExecutionError
from algen_agent_runtime.observability.safe_logging import runtime_logger
from algen_agent_runtime.security import redact_log_event
from algen_agent_runtime.types.contracts import RunStatus

CONTENT_CANARIES = {**SECRET_CANARIES, **PII_CANARIES}


class ErrorTracker:
    """A host processor that keeps what an error tracker (Sentry's structlog integration, say)
    would send: the event dict and the exception object, formatted."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def __call__(self, logger: Any, method: str, event: Any) -> Any:
        import traceback

        exc_info = event.get("exc_info")
        exc = exc_info[1] if isinstance(exc_info, tuple) else exc_info
        text = repr(dict(event))
        if isinstance(exc, BaseException):
            text += "".join(traceback.format_exception(exc))
        self.sent.append(text)
        return event


@pytest.fixture
def host_logging(capsys: pytest.CaptureFixture[str]) -> Iterator[ErrorTracker]:
    """structlog's default processors, with an error tracker in front of them, as a host that
    never calls configure_logging configures it."""
    previous = structlog.get_config()
    structlog.reset_defaults()
    tracker = ErrorTracker()
    defaults = list(structlog.get_config()["processors"])
    structlog.configure(processors=[tracker, *defaults])
    try:
        yield tracker
    finally:
        structlog.configure(**previous)


def _failing_tool_run() -> Any:
    return container(
        settings(
            agent_definition(enabled_tools=["billing.charge"], tool_policy={"on_error": "fail_run"})
        ),
        [tool("billing.charge", raising(RuntimeError(leaky_text("tool error"))))],
        [tool_call("billing.charge", {"query": "invoice 9"}), "sorry"],
    )


class Exploding:
    name = "golden.exploding"
    category = "custom"
    enforcement_mode = "enforce"

    async def evaluate(self, point: str, payload: Any, context: Any) -> Any:
        raise RuntimeError(leaky_text(f"policy {point}"))


@pytest.mark.parametrize("scenario", ["tool_error", "policy_error", "tool_execution_error"])
async def test_runtime_logs_are_masked_without_configure_logging(
    host_logging: ErrorTracker,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    scenario: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    if scenario == "policy_error":
        built = container(
            settings(
                agent_definition(
                    guardrail_policy={"policies": ["secrets", "pii", "golden.exploding"]}
                ),
                runtime={"recover_incomplete_runs": False, "component_check": "warn"},
            ),
            [],
            [leaky_text("answer")],
        )
        built.runtime.policies.register(Exploding())
    elif scenario == "tool_execution_error":
        built = container(
            settings(
                agent_definition(
                    enabled_tools=["billing.charge"], tool_policy={"on_error": "fail_run"}
                )
            ),
            [tool("billing.charge", raising(ToolExecutionError(leaky_text("tool error"))))],
            [tool_call("billing.charge", {"query": "invoice 9"}), "sorry"],
        )
    else:
        built = _failing_tool_run()

    result = await built.runtime.run(request(leaky_text("input")))
    assert result.status == RunStatus.FAILED

    printed = capsys.readouterr()
    sinks = {
        "console": printed.out + printed.err,
        "error_tracker": "\n".join(host_logging.sent),
        "stdlib": "\n".join(
            f"{record.getMessage()} {record.exc_text or ''}" for record in caplog.records
        ),
    }
    assert "agent_run_failed" in sinks["console"]
    # The traceback is still there, masked.
    assert "Traceback" in sinks["console"]
    if scenario != "policy_error":
        assert "[REDACTED]" in sinks["console"] and "[REDACTED_EMAIL]" in sinks["console"]
    assert_no_canary(sinks, CONTENT_CANARIES, scenario=f"host logging ({scenario})")
    assert_no_canary(sinks, HELD_CANARIES, scenario=f"host logging ({scenario}, held)")


async def test_a_host_that_installs_redact_log_event_gets_the_exception_object(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With ``redact_log_event`` in the host's chain, Runtime events pass through as they are:
    the host's own processors see the exception object, and the chain masks the output."""
    previous = structlog.get_config()
    tracker = ErrorTracker()
    seen: list[Any] = []

    def keep_exc_info(logger: Any, method: str, event: Any) -> Any:
        seen.append(event.get("exc_info"))
        return event

    structlog.configure(
        processors=[
            keep_exc_info,
            structlog.processors.format_exc_info,
            redact_log_event,
            tracker,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
    )
    try:
        try:
            raise RuntimeError(leaky_text("host chain"))
        except RuntimeError:
            runtime_logger("algen_agent_runtime.golden").exception("golden_failure")
    finally:
        structlog.configure(**previous)

    assert seen and seen[0] is True, "the exception reaches the host's processors as exc_info"
    printed = capsys.readouterr().out
    assert "golden_failure" in printed and "Traceback" in printed
    assert_no_canary(
        {"console": printed, "tracker": "\n".join(tracker.sent)},
        CONTENT_CANARIES,
        scenario="host chain with redact_log_event",
    )

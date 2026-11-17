"""Results table for the live provider contract suite, fed by hooks in ``tests/conftest.py``.

With ``ALGEN_LIVE_REPORT=<path>`` the run writes a Markdown table (one row per provider, one
column per check: ``pass``, ``FAIL``, ``xfail``, ``skip``) with the date and mode, ready to
paste into docs/provider-compatibility.md. It holds outcomes only, never request or response
content, and never a key.
"""

from __future__ import annotations

import datetime
import os
import re
from collections import defaultdict

import pytest

_NODE = re.compile(r"::(test_\w+)\[(\w+)(?:-(\w+))?\]$")
_results: dict[str, dict[str, str]] = defaultdict(dict)
_SHORT = {
    "test_generate": "generate",
    "test_stream": "stream",
    "test_stream_and_generate_count_the_same_input": "usage parity",
    "test_tool_call_round_trip": "tools",
    "test_structured_output": "json schema",
    "test_image_input": "images",
    "test_pdf_input": "pdf",
    "test_embeddings": "embeddings",
    "test_wrong_key_is_a_non_retryable_authentication_error": "wrong key",
    "test_unknown_model_is_a_non_retryable_invalid_request": "unknown model",
    "test_a_cancelled_live_stream_closes_the_connection": "cancel",
    "test_agent_run_with_a_tool": "agent run",
}


def record(report: pytest.TestReport) -> None:
    """Keep the outcome of one live test phase."""
    match = _NODE.search(report.nodeid)
    if match is None:
        return
    test, provider, variant = match.groups()  # ids are [provider] or [provider-variant]
    check = _SHORT.get(test, test.removeprefix("test_")) + (f" ({variant})" if variant else "")
    if report.when == "call" or report.outcome == "skipped" or report.failed:
        if hasattr(report, "wasxfail"):
            outcome = "xfail" if report.skipped else "XPASS"
        else:
            outcome = {"passed": "pass", "failed": "FAIL", "skipped": "skip"}[report.outcome]
        # A setup skip is final; a teardown failure overrides a pass.
        if report.when != "teardown" or outcome == "FAIL":
            _results[provider][check] = outcome


def write() -> None:
    """Write the table to ``ALGEN_LIVE_REPORT``, when it is set and a live test ran."""
    path = os.environ.get("ALGEN_LIVE_REPORT")
    if not path or not _results:
        return
    from live_support import MODE

    checks = sorted({check for row in _results.values() for check in row})
    lines = [
        f"Live provider contracts, {datetime.date.today().isoformat()}, mode `{MODE}`.",
        "",
        "| Provider | " + " | ".join(checks) + " |",
        "|---|" + "---|" * len(checks),
    ]
    for provider in sorted(_results):
        row = _results[provider]
        lines.append(f"| {provider} | " + " | ".join(row.get(c, "-") for c in checks) + " |")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

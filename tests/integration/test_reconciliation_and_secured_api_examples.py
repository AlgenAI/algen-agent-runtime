"""The side-effect reconciliation and secured API examples run offline and show what they claim."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def run_example(module: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", module],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    return result.stdout


def test_side_effect_reconciliation_example() -> None:
    lines = run_example("examples.pattern_side_effect_reconciliation.app").splitlines()

    # 1. Applied, then timed out: paused, settled by the reconcile hook, resumed.
    assert "run status: awaiting_reconciliation" in lines
    assert any(line.startswith("pause: kind=tool_reconciliation") for line in lines)
    assert "reconcile hook: outcome=completed run_resumed=True result_reference=cr-0001" in lines
    assert "run status: completed; output: Credited acct-1001 with 25.00." in lines
    assert "events: ['tool.reconciliation_required', 'tool.reconciled', 'tool.completed']" in lines
    # 2. Over HTTP: the scope is required, the hook cannot tell, the operator resolves it.
    assert "GET /v1/tool-executions without tools:reconcile -> 403" in lines
    assert "POST .../reconcile -> outcome=unknown" in lines
    assert any(
        line.startswith("POST .../resolve -> outcome=failed run_resumed=True actor=ops-maria")
        for line in lines
    )
    # 3. Compensation on request, recorded as compensating then compensated.
    assert "compensate: outcome=compensated status=compensated" in lines
    assert (
        "ledger decisions: ['reconcile:completed', 'compensate:compensating', "
        "'compensate:compensated']" in lines
    )
    assert "billing voided credits: ['cr-0001']" in lines
    # 4. A refusal before sending is failed, never indeterminate.
    assert "ledger: billing.issue_credit failed (error ToolNotAttemptedError)" in lines
    assert "requests sent to billing: 0" in lines
    assert "left to reconcile: 0" in lines


def test_secured_api_example() -> None:
    pytest.importorskip("jwt")
    pytest.importorskip("cryptography")
    output = run_example("examples.pattern_secured_api.app")
    lines = output.splitlines()

    assert (
        "carol: user=carol scopes=['approvals:read', 'auth:revoke', 'runs:read', 'runs:write']"
        in lines
    )
    assert "no token -> 401" in lines
    assert "token signed by another key -> 401" in lines
    assert "alice starts a run -> awaiting_approval" in lines
    assert "alice GET /v1/approvals -> 403 (missing scope approvals:read)" in lines
    assert any(
        line.startswith("carol GET /v1/approvals -> 1 pending") and "'[REDACTED]'" in line
        for line in lines
    )
    assert "carol modifies and approves -> 202; run completed" in lines
    assert (
        "inbox shows modified with {'url': 'https://hooks.acme.example/billing', "
        "'secret': '[REDACTED]'}" in lines
    )
    assert "alice GET /v1/runs/<her turn's run> -> 200" in lines
    assert "bob GET /v1/runs/<alice's turn's run> -> 404" in lines
    assert "bob GET /v1/conversations/<alice's> -> 404" in lines
    assert any(line.startswith("host route runs course-assistant") for line in lines)
    assert "LMS saw: ['alice']" in lines
    assert any(
        line.startswith("POST /v1/runs (no caller credential) -> failed")
        and "authentication required" in line
        for line in lines
    )
    assert "requests the LMS received for it: 0" in lines
    assert "bob after -> 401; alice -> 200" in lines
    # Generated secrets never reach the output; only the redaction marker does.
    assert "secret_fingerprint" not in output


def test_secured_api_example_explains_a_missing_auth_extra(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "jwt", None)
    from examples.pattern_secured_api.app import main

    main([])
    assert "pip install 'algen-agent-runtime[auth]'" in capsys.readouterr().out

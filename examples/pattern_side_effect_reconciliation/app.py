"""Unknown side-effect outcomes, offline: pause, reconcile, resolve, compensate.

Four runs of one billing agent against an in-process fake billing system:

1. The credit is applied, then the response times out. The run pauses; the tool's ``reconcile``
   hook finds the credit by idempotency key, and the run resumes with the recorded result.
2. The request times out before the credit is applied. Over the HTTP API (``tools:reconcile``),
   the hook cannot tell, so an operator resolves the call ``failed`` with a reason.
3. An operator asks for the first credit to be compensated: ``compensating`` -> ``compensated``.
4. The tool refuses a locked account with ``ToolNotAttemptedError`` before sending anything: the
   call is recorded ``failed``, not ``indeterminate``, and nothing needs reconciling.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import ToolExecutionStatus
from algen_agent_runtime.types.contracts import RunRequest, RunStatus
from examples.pattern_side_effect_reconciliation.tools import FakeBillingSystem, billing

CONFIG_PATH = Path(__file__).resolve().with_name("agent.yaml")
TENANT_ID = "example-tenant"
AGENT = "billing-assistant"
TOOL = "billing.issue_credit"
OPERATOR = "ops-maria"
# development_headers identities, for the in-process HTTP calls only.
OPERATOR_HEADERS = {"x-tenant-id": TENANT_ID, "x-user-id": OPERATOR, "x-scopes": "tools:reconcile"}
VIEWER_HEADERS = {"x-tenant-id": TENANT_ID, "x-user-id": "viewer-sam", "x-scopes": "runs:read"}


def say(text: str = "") -> None:
    print(text, flush=True)


async def start_credit(
    runtime: AgentRuntime, model: MockModelProvider, account_id: str, amount_cents: int, reply: str
) -> tuple[str, RunStatus]:
    """One run in which the scripted model issues one credit, then answers ``reply``."""
    model.queue(
        tool_call_response(TOOL, {"account_id": account_id, "amount_cents": amount_cents}), reply
    )
    result = await runtime.run(
        RunRequest(
            agent=AGENT,
            input=f"Credit {account_id} with {amount_cents} cents",
            tenant_id=TENANT_ID,
            user_id="agent-user",
        )
    )
    return result.run_id, result.status


async def tool_events(runtime: AgentRuntime, run_id: str) -> list[str]:
    return [
        event.type
        for event in await runtime.events.history(run_id)
        if event.type.startswith("tool.") and event.type not in {"tool.started", "tool.requested"}
    ]


async def run() -> dict[str, Any]:
    settings = load_settings((CONFIG_PATH,))
    # The settings declare BillingPlugin (plugin.py), which registers the billing toolkit.
    container = build_container(settings)
    model = container.runtime.router.provider("mock")
    assert isinstance(model, MockModelProvider)
    runtime = container.runtime
    reconciliation = runtime.reconciliation
    app = create_app(settings, container)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # no per-request log lines
    summary: dict[str, Any] = {}

    # The app's lifespan starts and stops the container, as under uvicorn.
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runtime.invalid"
        ) as http,
    ):
        # The toolkit's client, opened when the container started: the fake billing system.
        system: FakeBillingSystem = await billing.client()
        say("== 1. The credit was applied, then the response timed out ==")
        system.fault = "timeout_after_apply"
        first_run, status = await start_credit(
            runtime, model, "acct-1001", 2500, "Credited acct-1001 with 25.00."
        )
        state = await runtime.status(first_run, TENANT_ID)
        payload = state.pause_payload or {}
        first_key = str(payload["idempotency_key"])
        say(f"run status: {status.value}")
        say(f"pause: kind={payload['kind']} tool={payload['tool']} key={first_key}")

        records = await reconciliation.list(TENANT_ID)
        say(f"operator lists unknown calls: {[(r.tool_name, r.status.value) for r in records]}")
        report = await reconciliation.auto_reconcile(
            TENANT_ID, first_key, actor_id=OPERATOR, reason="ask billing by idempotency key"
        )
        recorded = report.record.result
        reference = recorded.metadata.get("result_reference") if recorded else None
        say(
            f"reconcile hook: outcome={report.outcome} run_resumed={report.run_resumed} "
            f"result_reference={reference}"
        )
        final = await runtime.wait(first_run, TENANT_ID)
        say(f"run status: {final.status.value}; output: {final.output}")
        say(f"events: {await tool_events(runtime, first_run)}")
        summary["first"] = {"status": final.status.value, "reconcile": report.outcome}

        say()
        say("== 2. The request timed out before it was applied (operator over HTTP) ==")
        system.fault = "timeout_before_apply"
        second_run, status = await start_credit(
            runtime, model, "acct-2002", 4000, "The credit did not go through; nothing was charged."
        )
        say(f"run status: {status.value}")
        refused = await http.get("/v1/tool-executions", headers=VIEWER_HEADERS)
        say(f"GET /v1/tool-executions without tools:reconcile -> {refused.status_code}")
        listed = (await http.get("/v1/tool-executions", headers=OPERATOR_HEADERS)).json()
        [execution] = listed["executions"]
        second_key = execution["idempotency_key"]
        say(
            f"GET /v1/tool-executions -> {execution['tool_name']} {execution['status']} "
            f"(error {execution['error']}, has_result {execution['has_result']})"
        )
        asked = await http.post(
            f"/v1/tool-executions/{second_key}/reconcile",
            headers=OPERATOR_HEADERS,
            json={"reason": "ask billing by idempotency key"},
        )
        say(f"POST .../reconcile -> outcome={asked.json()['outcome']}")
        resolved = await http.post(
            f"/v1/tool-executions/{second_key}/resolve",
            headers=OPERATOR_HEADERS,
            json={
                "status": "failed",
                "reason": "billing support confirms no credit for acct-2002",
                "expected_status": "indeterminate",
            },
        )
        body = resolved.json()
        decision = body["execution"]["resolutions"][-1]
        say(
            f"POST .../resolve -> outcome={body['outcome']} run_resumed={body['run_resumed']} "
            f"actor={decision['actor_id']} reason={decision['reason']!r}"
        )
        final = await runtime.wait(second_run, TENANT_ID)
        say(f"run status: {final.status.value}; output: {final.output}")
        summary["second"] = {"status": final.status.value, "resolve": body["outcome"]}

        say()
        say("== 3. Compensation, only when an operator asks ==")
        report = await reconciliation.compensate(
            TENANT_ID, first_key, actor_id=OPERATOR, reason="customer was credited twice"
        )
        steps = [f"{d.action}:{d.status.value}" for d in report.record.resolutions]
        say(f"compensate: outcome={report.outcome} status={report.record.status.value}")
        say(f"ledger decisions: {steps}")
        say(f"billing voided credits: {sorted(system.voided)}")
        summary["compensation"] = report.record.status.value

        say()
        say("== 4. Refused before anything was sent (ToolNotAttemptedError) ==")
        requests_before = system.requests
        fourth_run, status = await start_credit(
            runtime, model, "acct-9009", 1000, "That account is locked, so no credit was issued."
        )
        [record] = await reconciliation.list(
            TENANT_ID, statuses={ToolExecutionStatus.FAILED}, run_id=fourth_run
        )
        say(f"run status: {status.value}")
        say(f"ledger: {record.tool_name} {record.status.value} (error {record.error})")
        say(f"requests sent to billing: {system.requests - requests_before}")
        say(f"left to reconcile: {len(await reconciliation.list(TENANT_ID))}")
        summary["refused"] = record.status.value
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.parse_args(argv)
    asyncio.run(run())


if __name__ == "__main__":
    main()

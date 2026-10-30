"""A Runtime process of whichever release ``PYTHONPATH`` points at, for the upgrade and rollback
drills. It uses only interfaces ``0.1.0a17`` already had.

It reads its plan from ``DRILL_PLAN`` (JSON), starts a container on the drill database (which
migrates it as that release would), does one thing, stops gracefully and prints one line,
``RESULT {json}``:

    {"do": "write"}                 a run paused for approval and a completed run with a tool call
    {"do": "resume", "run_id": id, "approval_id": id}  approve the paused run, wait for its end
    {"do": "read", "run_id": id, "approval_id": id}    the run's status, approval, ledger, events
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from drill_support import AGENT, APPROVAL_AGENT, TENANT, USER, ledger, running

import algen_agent_runtime
from algen_agent_runtime.types.contracts import RunRequest, RunStatus


async def describe(
    process: object, dsn: str, run_id: str, approval_id: str | None
) -> dict[str, object]:
    runtime = process.runtime  # type: ignore[attr-defined]
    state = await runtime.status(run_id, TENANT)
    approval = await runtime.approvals.get(approval_id, TENANT) if approval_id else None
    return {
        "status": state.status.value,
        "output": state.output_text,
        "approval": approval.status.value if approval else None,
        "ledger": await ledger(dsn, run_id),
        "events": len(await runtime.events.history(run_id)),
    }


async def main() -> None:
    plan = json.loads(os.environ["DRILL_PLAN"])
    dsn = plan["dsn"]
    result: dict[str, object] = {"module": algen_agent_runtime.__file__}
    async with running(dsn) as process:
        runtime = process.runtime
        if plan["do"] == "write":
            done = await runtime.run(
                RunRequest(agent=AGENT, input="written", tenant_id=TENANT, user_id=USER)
            )
            paused = await runtime.start(
                RunRequest(agent=APPROVAL_AGENT, input="held", tenant_id=TENANT, user_id=USER)
            )
            for _ in range(2000):
                if (await runtime.status(paused.id, TENANT)).status is RunStatus.AWAITING_APPROVAL:
                    break
                await asyncio.sleep(0.01)
            approval_id = (await runtime.status(paused.id, TENANT)).pause_payload["approval_id"]
            result.update(completed=done.run_id, paused=paused.id, approval_id=approval_id)
            result["described"] = await describe(process, dsn, paused.id, approval_id)
        elif plan["do"] == "resume":
            await runtime.resume(plan["run_id"], TENANT, {"decision": "approved"})
            await runtime.wait(plan["run_id"], TENANT)
            result["described"] = await describe(
                process, dsn, plan["run_id"], plan.get("approval_id")
            )
        else:
            result["described"] = await describe(
                process, dsn, plan["run_id"], plan.get("approval_id")
            )
    sys.stdout.write("RESULT " + json.dumps(result) + "\n")


if __name__ == "__main__":
    asyncio.run(main())

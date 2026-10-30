"""A Runtime process for the crash drills; the drill kills it with SIGKILL.

It reads its plan from ``DRILL_PLAN`` (JSON), starts a Runtime container on the drill database
(which recovers whatever the previous process left), starts the runs, workflows and conversation
turns the plan asks for, reports each on stdout and then waits to be killed:

    READY                     the container started (and recovered)
    RUN <run_id>              a run was started
    WORKFLOW <workflow_id>    a workflow run was started
    TURN <conversation_id>    a conversation turn was submitted
    PAUSED <run_id>           a run is waiting for approval
    BLOCKED <where> <hint>    a run reached the gate (see drill_support.Gate)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from drill_support import (
    APPROVAL_AGENT,
    TENANT,
    USER,
    WORKFLOW,
    Gate,
    running,
)

from algen_agent_runtime.types.contracts import RunRequest, RunStatus


def announce(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


async def main() -> None:
    plan = json.loads(os.environ["DRILL_PLAN"])
    gate = Gate(
        after_tool="after_tool" in plan.get("block", ()),
        in_tool="in_tool" in plan.get("block", ()),
        announce=announce,
    )
    async with running(plan["dsn"], gate=gate) as process:
        announce("READY")
        runtime = process.runtime
        for item in plan.get("runs", []):
            state = await runtime.start(
                RunRequest(agent=item["agent"], input=item["input"], tenant_id=TENANT, user_id=USER)
            )
            announce(f"RUN {state.id}")
            if item["agent"] == APPROVAL_AGENT:
                for _ in range(2000):
                    current = await runtime.status(state.id, TENANT)
                    if current.status is RunStatus.AWAITING_APPROVAL:
                        announce(f"PAUSED {state.id}")
                        break
                    await asyncio.sleep(0.01)
        workflows = process.container.lifecycle.workflows
        for text in plan.get("workflows", []):
            state = await workflows.start(WORKFLOW, tenant_id=TENANT, user_id=USER, input_text=text)
            announce(f"WORKFLOW {state.id}")
        for item in plan.get("turns", []):
            conversations = process.container.conversations
            conversation = await conversations.create(
                tenant_id=TENANT, user_id=USER, agent=item["agent"]
            )
            await conversations.submit(conversation.id, TENANT, USER, item["input"])
            announce(f"TURN {conversation.id}")
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())

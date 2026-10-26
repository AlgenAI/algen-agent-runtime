"""Run the CRM assistant offline: a read, then a write that waits for approval."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.types.contracts import RunRequest, RunStatus

CONFIG_PATH = Path(__file__).resolve().with_name("agent.yaml")
TENANT_ID = "example-tenant"


async def run(owner: str = "sam", decision: str = "approved") -> dict[str, Any]:
    # The settings declare CrmPlugin (plugin.py), which registers the crm toolkit.
    container = build_container(load_settings((CONFIG_PATH,)))
    model = container.runtime.router.provider("demo")
    assert isinstance(model, MockModelProvider)
    # Script the model offline: look the account up, change its owner, then answer.
    model.queue(
        tool_call_response("crm.find_account", {"domain": "acme.com"}),
        tool_call_response("crm.update_owner", {"account_id": "acc-1", "owner": owner}),
        f"acme.com now belongs to {owner}.",
    )
    try:
        await container.astart()
        result = await AlgenAgentRuntimeClient(container.runtime).run(
            RunRequest(
                agent="crm-assistant",
                input=f"Make {owner} the owner of acme.com",
                tenant_id=TENANT_ID,
                user_id="example-user",
            )
        )
        paused = result.status
        if paused == RunStatus.AWAITING_APPROVAL:
            await container.runtime.resume(result.run_id, TENANT_ID, {"decision": decision})
        final = await container.runtime.wait(result.run_id, TENANT_ID)
        return {
            "paused_for": paused.value,
            "status": final.status.value,
            "output": final.output,
            "error": final.error,
        }
    finally:
        await container.aclose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the CRM tool quickstart offline")
    parser.add_argument("owner", nargs="?", default="sam")
    parser.add_argument("--reject", action="store_true", help="Reject the owner change")
    args = parser.parse_args(argv)
    print(asyncio.run(run(args.owner, "rejected" if args.reject else "approved")))


if __name__ == "__main__":
    main()

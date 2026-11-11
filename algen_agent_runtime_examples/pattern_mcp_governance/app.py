from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.policies.contracts import PolicyAction
from algen_agent_runtime.policies.engine import SideEffectPolicy
from algen_agent_runtime.types.contracts import ApprovalPolicy


async def run_mcp_governance_demonstration() -> dict[str, Any]:
    """Demonstrate policy evaluation on MCP tools.

    Proves that:
    1. The read-only tool (`read_account`) is classified with `side_effect: read`
       and runs without approval (PolicyAction.ALLOW).
    2. The side-effecting/destructive tool (`delete_account`) is classified with
       `side_effect: destructive` and requires approval (PolicyAction.REQUIRE_APPROVAL).
    """
    config_path = Path(__file__).with_name("agent.yaml")
    settings = load_settings((config_path,))

    # Ensure command uses current Python interpreter to run the server module
    mcp_servers = list(settings.mcp_servers)
    if mcp_servers:
        server_cfg = mcp_servers[0]
        updated_server = server_cfg.model_copy(update={"command": sys.executable})
        mcp_servers[0] = updated_server
        settings = settings.model_copy(update={"mcp_servers": tuple(mcp_servers)})

    container = build_container(settings)
    await container.astart()

    try:
        read_tool = container.tools.get("mcp.account_service.read_account")
        delete_tool = container.tools.get("mcp.account_service.delete_account")

        approval_policy = ApprovalPolicy(
            require_for_side_effects=True,
            require_for_destructive=True,
        )

        policy_engine = SideEffectPolicy()

        # Evaluate read-only tool
        read_decision = await policy_engine.evaluate(
            point="plan_tool",
            payload={"tool": read_tool.definition.name},
            context={"tool": read_tool.definition, "approval_policy": approval_policy},
        )

        # Evaluate destructive/side-effecting tool
        delete_decision = await policy_engine.evaluate(
            point="plan_tool",
            payload={"tool": delete_tool.definition.name},
            context={"tool": delete_tool.definition, "approval_policy": approval_policy},
        )

        return {
            "read_tool": {
                "name": read_tool.definition.name,
                "side_effect": read_tool.definition.side_effect.value,
                "decision": read_decision.action.value,
                "requires_approval": read_decision.action == PolicyAction.REQUIRE_APPROVAL,
            },
            "delete_tool": {
                "name": delete_tool.definition.name,
                "side_effect": delete_tool.definition.side_effect.value,
                "decision": delete_decision.action.value,
                "requires_approval": delete_decision.action == PolicyAction.REQUIRE_APPROVAL,
            },
        }
    finally:
        await container.aclose()


def main() -> None:
    results = asyncio.run(run_mcp_governance_demonstration())
    print(
        f"Read Tool: {results['read_tool']['name']} -> "
        f"Decision: {results['read_tool']['decision']} "
        f"(Requires approval: {results['read_tool']['requires_approval']})"
    )
    print(
        f"Delete Tool: {results['delete_tool']['name']} -> "
        f"Decision: {results['delete_tool']['decision']} "
        f"(Requires approval: {results['delete_tool']['requires_approval']})"
    )


if __name__ == "__main__":
    main()

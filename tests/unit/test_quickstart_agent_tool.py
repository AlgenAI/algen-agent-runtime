from __future__ import annotations

from pathlib import Path

import pytest

from algen_agent_runtime.orchestration.check import check_project
from algen_agent_runtime.tools.testing import assert_tool_contract, fake_tool_context
from examples.quickstart_agent_tool.app import CONFIG_PATH, run
from examples.quickstart_agent_tool.tools import crm, find_account, update_owner


@pytest.mark.parametrize("tool", crm.tools, ids=lambda tool: tool.definition.name)
def test_each_tool_meets_the_contract(tool: object) -> None:
    assert_tool_contract(tool)  # type: ignore[arg-type]


def test_the_project_passes_check() -> None:
    report = check_project((CONFIG_PATH,), project_root=Path(CONFIG_PATH).parent)

    assert report.ok
    assert [issue.code for issue in report.warnings] == ["OWN_CONTAINER"]
    tools = {tool.name: tool for tool in report.inventory.tools}  # type: ignore[union-attr]
    assert tools["crm.update_owner"].requires_approval is True
    assert tools["crm.update_owner"].source == "examples.quickstart_agent_tool.plugin:CrmPlugin"


async def test_an_approved_change_completes_and_a_rejected_one_fails() -> None:
    approved = await run("sam")
    rejected = await run("sam", "rejected")

    assert approved == {
        "paused_for": "awaiting_approval",
        "status": "completed",
        "output": "acme.com now belongs to sam.",
        "error": None,
    }
    assert (rejected["status"], rejected["error"]) == ("failed", "Proposed action was rejected.")


async def test_a_replayed_write_returns_the_first_receipt() -> None:
    context = fake_tool_context(update_owner)
    try:
        first = await update_owner.execute({"account_id": "acc-1", "owner": "sam"}, context)
        again = await update_owner.execute({"account_id": "acc-1", "owner": "kim"}, context)
        found = await find_account.execute({"domain": "acme.com"}, fake_tool_context(find_account))
    finally:
        await crm.aclose()

    assert first == again == {"account_id": "acc-1", "owner": "sam", "previous_owner": "dana"}
    assert found["owner"] == "sam"

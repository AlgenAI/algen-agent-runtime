"""Connectors that moved to algen-agent-tools warn on use until they are removed in 0.2."""

from __future__ import annotations

import importlib
import sys
from typing import Any

import pytest

from algen_agent_runtime.tools.contracts import ToolContext


def _forget_communications(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(sys.modules):
        if name == "algen_agent_runtime.communications" or name.startswith(
            "algen_agent_runtime.communications."
        ):
            monkeypatch.delitem(sys.modules, name)


@pytest.mark.parametrize(
    "module",
    [
        "algen_agent_runtime.communications",
        "algen_agent_runtime.communications.email",
        "algen_agent_runtime.communications.gmail",
    ],
)
def test_importing_communications_warns_with_the_replacement(
    monkeypatch: pytest.MonkeyPatch, module: str
) -> None:
    _forget_communications(monkeypatch)

    with pytest.warns(DeprecationWarning, match="algen_agent_tools.smtp") as caught:
        importlib.import_module(module)

    [warning] = [item for item in caught if "communications is deprecated" in str(item.message)]
    assert warning.filename == __file__


def test_communications_still_works_while_deprecated(monkeypatch: pytest.MonkeyPatch) -> None:
    _forget_communications(monkeypatch)
    with pytest.warns(DeprecationWarning):
        communications = importlib.import_module("algen_agent_runtime.communications")

    message = communications.EmailMessage.model_validate(
        {
            "sender": {"address": "desk@example.org"},
            "to": [{"address": "person@example.com"}],
            "subject": "Still works",
            "text_body": "Until 0.2.",
        }
    )
    assert message.subject == "Still works"


def test_database_tool_warns_at_the_call_site() -> None:
    from algen_agent_runtime.tools.adapters import database_tool

    async def handler(statement: str, parameters: Any, context: ToolContext) -> Any:
        return []

    with pytest.warns(DeprecationWarning, match="PostgresToolkit.query") as caught:
        tool = database_tool("db.query", handler)

    assert caught[0].filename == __file__
    assert tool.definition.name == "db.query"


def test_the_remaining_adapters_do_not_warn(recwarn: pytest.WarningsRecorder) -> None:
    from algen_agent_runtime.tools.adapters import mcp_tool, remote_tool  # noqa: F401

    assert not [item for item in recwarn if item.category is DeprecationWarning]

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


@pytest.mark.parametrize("name", ["GmailMailboxClient", "gmail_inbox_tool"])
def test_the_removed_gmail_adapter_points_to_algen_agent_tools(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _forget_communications(monkeypatch)
    with pytest.warns(DeprecationWarning):
        communications = importlib.import_module("algen_agent_runtime.communications")

    with pytest.raises(ImportError, match=r"removed in 0\.1\.0a12.*algen_agent_tools\.gmail"):
        getattr(communications, name)
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("algen_agent_runtime.communications.gmail")


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


async def test_the_deprecated_email_tool_sends_under_the_default_pii_guardrail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algen_agent_runtime.policies.engine import CompositePolicyEngine
    from algen_agent_runtime.tools.executor import ToolExecutor
    from algen_agent_runtime.tools.registry import ToolRegistry
    from algen_agent_runtime.tools.testing import fake_tool_context

    _forget_communications(monkeypatch)
    with pytest.warns(DeprecationWarning):
        communications = importlib.import_module("algen_agent_runtime.communications")
    sent: list[Any] = []

    class Sender:
        async def send(self, message: Any, *, idempotency_key: str) -> Any:
            sent.append(message)
            return communications.EmailDeliveryReceipt(
                provider="fake",
                provider_message_id="m1",
                status=communications.EmailDeliveryStatus.ACCEPTED,
                accepted_recipients=tuple(item.address for item in message.to),
            )

    item = communications.email_tool(Sender())
    registry = ToolRegistry()
    registry.register(item)
    await ToolExecutor(registry, CompositePolicyEngine()).execute(
        item.definition.name,
        {
            "sender": {"address": "desk@example.org"},
            "to": [{"address": "kim@example.com"}],
            "subject": "Shipped",
            "text_body": "Your order shipped.",
        },
        fake_tool_context(item),
    )

    assert [message.to[0].address for message in sent] == ["kim@example.com"]


@pytest.mark.parametrize(
    "module",
    [
        "algen_agent_runtime.security.documents",
        "algen_agent_runtime.security.docx",
        "algen_agent_runtime.security.pptx",
        "algen_agent_runtime.security.xlsx",
        "algen_agent_runtime.security.html",
    ],
)
def test_importing_a_document_extractor_warns_with_the_replacement(
    monkeypatch: pytest.MonkeyPatch, module: str
) -> None:
    """The extractors move to algen-agent-tools (#81); they keep working meanwhile."""
    for name in list(sys.modules):
        if name.startswith("algen_agent_runtime.security.") and name != (
            "algen_agent_runtime.security._moved"
        ):
            monkeypatch.delitem(sys.modules, name)

    with pytest.warns(DeprecationWarning, match="algen_agent_tools.documents") as caught:
        importlib.import_module(module)

    [warning] = [item for item in caught if f"{module} is deprecated" in str(item.message)]
    assert warning.filename == __file__


def test_the_runtime_core_does_not_import_the_document_extractors() -> None:
    import subprocess

    code = (
        "import sys, algen_agent_runtime.api.app, algen_agent_runtime.orchestration.container;"
        "print(sorted(m for m in sys.modules if m.startswith('algen_agent_runtime.security.')"
        " and m.rsplit('.', 1)[1] in {'documents','docx','pptx','xlsx','html','ooxml'}))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert loaded == "[]"

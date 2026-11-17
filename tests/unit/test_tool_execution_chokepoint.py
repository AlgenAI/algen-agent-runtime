"""Only the ToolExecutor runs a tool (0.2.0b5).

The executor is where policy, approval, budgets, the idempotency ledger and redaction apply, and
it refuses a tool that ``requires_approval`` without a decided approval. Code that called a
tool's handler itself would skip all of that, as the framework tool bridge once ran
side-effecting tools without approval (GOV-2 in the 0.2.0b3 security assessment). This test reads
the source: a tool's ``handler``, ``execute``, ``compensate`` or ``reconcile`` is used only by
the modules listed below. A new way to run tools calls ``ToolExecutor.execute`` instead.

See AGENTS.md, "Security invariants".
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src" / "algen_agent_runtime"

# Each module, with what it may touch and why.
ALLOWED: dict[str, set[str]] = {
    # The Tool class itself and wrap(): they define and decorate the handler.
    "tools/contracts.py": {"handler", "execute", "compensate", "reconcile", "compensation"},
    # The governed path.
    "tools/executor.py": {"handler", "reconcile"},
    # Checks a process-isolated handler can be pickled when the tool is registered.
    "tools/registry.py": {"handler"},
    # Operator reconciliation and compensation, behind the tool-execution scopes and the ledger.
    "runtime/reconciliation.py": {"reconcile", "compensation", "compensate"},
    # Reports whether a tool has the hooks; calls neither.
    "orchestration/describe.py": {"reconcile", "compensation"},
}
ATTRIBUTES = {"handler", "execute", "compensate", "reconcile", "compensation"}


def _receiver(node: ast.Attribute) -> str:
    """The expression the attribute is read from: ``self.tools.get(name)`` in
    ``self.tools.get(name).handler``."""
    return ast.unparse(node.value)


def _uses() -> list[tuple[str, int, str]]:
    found = []
    for path in sorted(SOURCE.rglob("*.py")):
        module = path.relative_to(SOURCE).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Attribute) or node.attr not in ATTRIBUTES:
                continue
            receiver = _receiver(node).lower()
            # A tool, by the names the code base gives them; executors, SQL connections,
            # conversations and workflow nodes have handlers or execute methods of their own.
            if "tool" not in receiver or "executor" in receiver:
                continue
            if node.attr in ALLOWED.get(module, set()):
                continue
            found.append((module, node.lineno, ast.unparse(node)))
    return found


def test_only_the_executor_runs_a_tool() -> None:
    uses = _uses()
    assert not uses, (
        "Run tools through ToolExecutor.execute (with the approval that covers the call), not "
        f"through the tool's handler: {uses}"
    )


def test_the_check_sees_a_direct_call() -> None:
    """The check finds the shapes a bypass takes."""
    for source in (
        "async def f(tool, a, c):\n    return await tool.execute(a, c)\n",
        "def f(tools, n, a, c):\n    return tools[n].handler(a, c)\n",
        "def f(self, n, a, c):\n    return self.tools.get(n).handler(a, c)\n",
    ):
        nodes = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Attribute) and node.attr in ATTRIBUTES
        ]
        assert any("tool" in _receiver(node).lower() for node in nodes), source

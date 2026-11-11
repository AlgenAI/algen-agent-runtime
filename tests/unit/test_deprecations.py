"""Connectors that moved to algen-agent-tools warn on use until they are removed in 0.2."""

from __future__ import annotations

import importlib
import sys

import pytest


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

"""Interfaces deprecated during 0.1 and removed in 0.2 stay removed."""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "algen_agent_runtime.communications",
        "algen_agent_runtime.communications.email",
        "algen_agent_runtime.security.documents",
        "algen_agent_runtime.security.docx",
        "algen_agent_runtime.security.docx_contrast",
        "algen_agent_runtime.security.ooxml",
        "algen_agent_runtime.security.pptx",
        "algen_agent_runtime.security.xlsx",
        "algen_agent_runtime.security.html",
    ],
)
def test_a_removed_module_is_not_importable(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_database_tool_is_removed() -> None:
    adapters = importlib.import_module("algen_agent_runtime.tools.adapters")

    assert not hasattr(adapters, "database_tool")


def test_the_runtime_core_imports_without_deprecation_warnings() -> None:
    code = (
        "import warnings; warnings.simplefilter('error', DeprecationWarning);"
        "import algen_agent_runtime, algen_agent_runtime.api.app,"
        " algen_agent_runtime.orchestration.container, algen_agent_runtime.tools.adapters"
    )
    subprocess.run([sys.executable, "-c", code], check=True)

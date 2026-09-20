"""Smoke tests for the quickstart_rag example."""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.skipif(
    not __import__("os").getenv("OPENAI_API_KEY")
    and not __import__("os").getenv("MISTRAL_API_KEY"),
    reason="Requires OPENAI_API_KEY or MISTRAL_API_KEY",
)
def test_openai_annual_leave() -> None:
    """Agent returns a grounded answer about annual leave with at least one citation."""
    from examples.quickstart_rag.app import ask

    result = asyncio.run(ask("How much annual leave do full-time employees receive?"))
    assert result.output, "Expected non-empty output"
    assert result.citations, "Expected at least one citation"
    assert any("annual" in (c.title or "").lower() for c in result.citations), (
        "Expected a citation related to annual leave"
    )


@pytest.mark.skipif(
    not __import__("os").getenv("OPENAI_API_KEY")
    and not __import__("os").getenv("MISTRAL_API_KEY"),
    reason="Requires OPENAI_API_KEY or MISTRAL_API_KEY",
)
def test_config_loads_without_error() -> None:
    """Agent configuration loads and container builds without raising."""
    from pathlib import Path

    from algen_agent_runtime.config.settings import load_settings
    from algen_agent_runtime.orchestration.container import build_container

    example_dir = Path(__file__).resolve().parent.parent
    settings = load_settings((example_dir / "agent.yaml",))
    container = build_container(settings)
    container.close()

from __future__ import annotations

import os

import pytest

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.orchestration.container import build_container
from examples.quickstart_rag.app import AGENT_BY_PROVIDER, EXAMPLE_DIR, ask, parse_args


def test_rag_example_configuration_wires_both_model_providers() -> None:
    settings = load_settings((EXAMPLE_DIR / "agent.yaml",))
    agents = {agent.name: agent for agent in settings.agents}

    assert agents[AGENT_BY_PROVIDER["openai"]].context_builder == ("retrieval.company-handbook")
    assert agents[AGENT_BY_PROVIDER["mistral"]].default_model.provider == "mistral"
    assert agents[AGENT_BY_PROVIDER["mistral"]].guardrail_policy.require_citations
    container = build_container(settings)
    assert container.retrievers.list() == ("company-handbook",)


def test_rag_example_cli_selects_mistral() -> None:
    arguments = parse_args(["--provider", "mistral", "When is support available?"])
    assert arguments.provider == "mistral"
    assert arguments.question == "When is support available?"
    assert not arguments.offline


@pytest.mark.parametrize("provider", ["openai", "mistral"])
async def test_rag_example_offline_answer_is_grounded_and_cited(provider: str) -> None:
    result = await ask(
        "How much annual leave do full-time employees receive?",
        provider,  # type: ignore[arg-type]
        offline=True,
    )

    assert result.error is None
    assert "twenty days" in (result.output or "")
    assert result.citations[0].title == "Annual leave policy"


# Formerly examples/quickstart_rag/tests/test_smoke.py, which pytest never collected.
@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="Requires OPENAI_API_KEY")
async def test_rag_example_live_openai_answer_cites_the_leave_policy() -> None:
    result = await ask("How much annual leave do full-time employees receive?")

    assert result.output, "Expected non-empty output"
    assert any("annual" in (citation.title or "").lower() for citation in result.citations)

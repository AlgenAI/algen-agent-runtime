from __future__ import annotations

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.orchestration.container import build_container
from examples.quickstart_rag.app import AGENT_BY_PROVIDER, EXAMPLE_DIR, parse_args


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

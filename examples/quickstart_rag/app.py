from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.types.contracts import RunRequest, RunResult
from examples.offline import offline_settings, require_key, script

EXAMPLE_DIR = Path(__file__).resolve().parent
ProviderName = Literal["openai", "mistral"]
AGENT_BY_PROVIDER: dict[ProviderName, str] = {
    "openai": "handbook-rag",
    "mistral": "handbook-rag-mistral",
}
KEY_BY_PROVIDER: dict[ProviderName, str] = {
    "openai": "OPENAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}
# Offline, the scripted model answers from the handbook topic the question names and cites the
# first retrieved source, [S1], which is that topic's document.
OFFLINE_ANSWERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("leave", "holiday", "vacation"),
        "Full-time employees receive twenty days of paid annual leave per calendar year [S1].",
    ),
    (
        ("support", "hours", "weekend"),
        "Standard support runs Monday to Friday, 09:00 to 18:00 UTC; priority-one incidents are monitored continuously [S1].",
    ),
    (
        ("incident", "security", "breach"),
        "Report a suspected security incident immediately through the security hotline and preserve the evidence [S1].",
    ),
)
OFFLINE_FALLBACK = (
    "The handbook evidence is insufficient to answer that; the closest source is [S1]."
)


def offline_answer(question: str) -> str:
    words = question.lower()
    for keywords, answer in OFFLINE_ANSWERS:
        if any(keyword in words for keyword in keywords):
            return answer
    return OFFLINE_FALLBACK


async def ask(
    question: str, provider: ProviderName = "openai", *, offline: bool = False
) -> RunResult:
    config = (EXAMPLE_DIR / "agent.yaml",)
    settings = offline_settings(config, AGENT_BY_PROVIDER) if offline else load_settings(config)
    container = build_container(settings)
    if offline:
        script(container, provider, offline_answer(question))
    try:
        return await AlgenAgentRuntimeClient(container.runtime).run(
            RunRequest(
                agent=AGENT_BY_PROVIDER[provider],
                input=question,
                tenant_id="example-tenant",
                user_id="example-user",
            )
        )
    finally:
        await container.aclose()


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask the handbook RAG agent a question.")
    parser.add_argument(
        "question",
        nargs="?",
        default="How much annual leave do full-time employees receive?",
    )
    parser.add_argument("--provider", choices=tuple(AGENT_BY_PROVIDER), default="openai")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use the deterministic mock provider with a scripted answer; needs no API key.",
    )
    return parser.parse_args(arguments)


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parse_args(argv)
    if arguments.offline:
        print("[offline: scripted mock model; retrieval and citation checks are real]")
    else:
        require_key(KEY_BY_PROVIDER[arguments.provider])
    result = asyncio.run(ask(arguments.question, arguments.provider, offline=arguments.offline))
    if result.error:
        raise RuntimeError(result.error)
    print(result.output or "")
    if result.citations:
        print("\nSources:")
        for citation in result.citations:
            target = citation.uri or citation.source
            print(f"- [{citation.id}] {citation.title or citation.source}: {target}")


if __name__ == "__main__":
    main()

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

EXAMPLE_DIR = Path(__file__).resolve().parent
ProviderName = Literal["openai", "mistral"]
AGENT_BY_PROVIDER: dict[ProviderName, str] = {
    "openai": "handbook-rag",
    "mistral": "handbook-rag-mistral",
}


async def ask(question: str, provider: ProviderName = "openai") -> RunResult:
    settings = load_settings((EXAMPLE_DIR / "agent.yaml",))
    container = build_container(settings)
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
        container.close()


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask the handbook RAG agent a question.")
    parser.add_argument(
        "question",
        nargs="?",
        default="How much annual leave do full-time employees receive?",
    )
    parser.add_argument("--provider", choices=tuple(AGENT_BY_PROVIDER), default="openai")
    return parser.parse_args(arguments)


def main() -> None:
    arguments = parse_args()
    result = asyncio.run(ask(arguments.question, arguments.provider))
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

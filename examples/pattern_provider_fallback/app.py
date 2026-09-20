from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.types.contracts import RunRequest, RunResult

EXAMPLE_DIR = Path(__file__).resolve().parent


async def ask(message: str) -> RunResult:
    """Run a message through the resilient multi-provider agent.

    The agent will attempt the primary Anthropic provider first, fall back to
    OpenAI on failure, and finally fall back to a local Ollama model. The
    selected provider route is visible in the run result metadata.
    """
    settings = load_settings((EXAMPLE_DIR / "agent.yaml",))
    container = build_container(settings)
    try:
        return await AlgenAgentRuntimeClient(container.runtime).run(
            RunRequest(
                agent="resilient",
                input=message,
                tenant_id="example-tenant",
                user_id="example-user",
            )
        )
    finally:
        container.close()


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Multi-provider fallback pattern. Attempts Anthropic → OpenAI → Ollama "
            "in order and prints the selected route and result."
        )
    )
    parser.add_argument(
        "message",
        nargs="?",
        default="What year did the first moon landing occur?",
        help="Message to send (default: a simple factual question).",
    )
    return parser.parse_args(arguments)


def main() -> None:
    arguments = parse_args()
    result = asyncio.run(ask(arguments.message))
    if result.error:
        raise RuntimeError(result.error)
    if result.model_used:
        print(f"[Route: {result.model_used}]")
    print(result.output or "")


if __name__ == "__main__":
    main()

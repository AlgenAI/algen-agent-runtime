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

ProviderMode = Literal["ollama", "ollama-private"]
AGENT_BY_MODE: dict[ProviderMode, str] = {
    "ollama": "minimal",
    "ollama-private": "local-private",
}


async def chat(message: str, mode: ProviderMode = "ollama") -> RunResult:
    """Run a single message through the local Ollama agent.

    Args:
        message: The user message to send.
        mode: Provider mode.  ``"ollama"`` uses the default llama3.2 model;
            ``"ollama-private"`` uses qwen3:8b with a strict model allowlist.
    """
    settings = load_settings((EXAMPLE_DIR / "agent.yaml",))
    container = build_container(settings)
    try:
        return await AlgenAgentRuntimeClient(container.runtime).run(
            RunRequest(
                agent=AGENT_BY_MODE[mode],
                input=message,
                tenant_id="quickstart",
                user_id="local-user",
            )
        )
    finally:
        container.close()


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Five-minute local chat quickstart. Requires Ollama to be running on "
            "http://127.0.0.1:11434 with the selected model pulled."
        )
    )
    parser.add_argument(
        "message",
        nargs="?",
        default="What is the capital of France?",
        help="Message to send to the agent (default: asks a simple geography question).",
    )
    parser.add_argument(
        "--mode",
        choices=tuple(AGENT_BY_MODE),
        default="ollama",
        help=(
            "Provider mode. 'ollama' uses llama3.2 (default). "
            "'ollama-private' uses qwen3:8b with a strict model allowlist."
        ),
    )
    return parser.parse_args(arguments)


def main() -> None:
    arguments = parse_args()
    result = asyncio.run(chat(arguments.message, arguments.mode))
    if result.error:
        raise RuntimeError(result.error)
    print(result.output or "")


if __name__ == "__main__":
    main()

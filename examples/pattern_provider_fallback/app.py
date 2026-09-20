from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.exceptions.errors import ProviderError
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.types.contracts import (
    ErrorKind,
    Message,
    ModelCapabilities,
    ModelProfile,
    ModelRequest,
    Role,
    RunRequest,
    RunResult,
)

EXAMPLE_DIR = Path(__file__).resolve().parent


class PlannedFailureProvider:
    """Provider used only by the deterministic routing demonstration."""

    provider_id = "planned-failure"

    async def capabilities(self, model: str) -> ModelCapabilities:
        del model
        return ModelCapabilities(chat=True)

    async def generate(self, request: ModelRequest) -> Any:
        del request
        raise ProviderError("planned primary failure", ErrorKind.UNAVAILABLE, retryable=True)

    async def health(self) -> bool:
        return False


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


async def deterministic_fallback(message: str) -> tuple[str, str]:
    """Prove fallback without credentials, network access, or invalid endpoints."""
    router = ModelRouter(circuit_failure_threshold=1)
    router.register_provider(PlannedFailureProvider())
    router.register_provider(MockModelProvider(responses=("deterministic fallback response",)))
    response = await router.generate(
        ModelRequest(messages=(Message.text(Role.USER, message),)),
        (
            ModelProfile(
                name="primary", provider="planned-failure", model="primary", quality_tier=3
            ),
            ModelProfile(name="fallback", provider="mock", model="deterministic", quality_tier=2),
        ),
    )
    return f"{response.provider}/{response.model}", response.message.text_content


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
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help="Use a planned primary failure and in-memory fallback; requires no credentials.",
    )
    return parser.parse_args(arguments)


def main() -> None:
    arguments = parse_args()
    if arguments.deterministic:
        route, output = asyncio.run(deterministic_fallback(arguments.message))
        print(f"[Route: {route}]")
        print(output)
        return
    result = asyncio.run(ask(arguments.message))
    if result.error:
        raise RuntimeError(result.error)
    if result.model_used:
        print(f"[Route: {result.model_used}]")
    print(result.output or "")


if __name__ == "__main__":
    main()

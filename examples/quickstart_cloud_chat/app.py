from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.types.contracts import RunRequest
from examples.offline import offline_settings, require_key, script

CONFIG_PATH = Path(__file__).with_name("agent.yaml")
OFFLINE_ANSWER = (
    "A governed agent is a model-driven assistant whose tools, budgets, approvals and outputs "
    "are checked and recorded by the runtime it runs in."
)


async def chat(message: str, *, offline: bool = False) -> str:
    settings = offline_settings((CONFIG_PATH,), ("openai",)) if offline else None
    container = build_container(settings or load_settings((CONFIG_PATH,)))
    if offline:
        script(container, "openai", OFFLINE_ANSWER)
    try:
        result = await AlgenAgentRuntimeClient(container.runtime).run(
            RunRequest(agent="cloud-chat", input=message, tenant_id="quickstart", user_id="user")
        )
        if result.error:
            raise RuntimeError(result.error)
        return result.output or ""
    finally:
        await container.aclose()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the cloud-chat quickstart")
    parser.add_argument("message", nargs="?", default="Explain governed agents in one sentence.")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use the deterministic mock provider with a scripted answer; needs no API key.",
    )
    arguments = parser.parse_args(argv)
    if arguments.offline:
        print("[offline: scripted mock model; guardrails, verification and budgets are real]")
    else:
        require_key("OPENAI_API_KEY")
    print(asyncio.run(chat(arguments.message, offline=arguments.offline)))


if __name__ == "__main__":
    main()

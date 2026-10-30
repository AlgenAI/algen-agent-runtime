from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.types.contracts import RunRequest, RunResult
from examples.offline import offline_settings, require_key, script

EXAMPLE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = EXAMPLE_DIR / "config" / "agent.yaml"
UI_PATH = EXAMPLE_DIR / "ui" / "index.html"
# Offline, the scripted tutor answers from the first retrieved course source, [S1].
OFFLINE_ANSWER = (
    "Accuracy is one number about one test set. Responsible AI also asks who the errors fall on, "
    "whether people affected can understand and contest a decision, and who is accountable for "
    "it, so a model needs fairness, explanation and oversight checks too [S1]. Which of these "
    "would you like to practise first?"
)


async def ask(question: str, *, offline: bool = False) -> RunResult:
    # The settings declare TeachingPlugin, which registers the teaching.* tools.
    settings = (
        offline_settings((CONFIG_PATH,), ("openai",)) if offline else load_settings((CONFIG_PATH,))
    )
    container = build_container(settings)
    if offline:
        script(container, "openai", OFFLINE_ANSWER)
    try:
        await container.astart()
        run = container.observability.govern(
            AlgenAgentRuntimeClient(container.runtime).run,
            agent_id="virtual-teaching-assistant",
            agent_name="virtual-teaching-assistant",
        )
        return cast(
            RunResult,
            await run(
                RunRequest(
                    agent="virtual-teaching-assistant",
                    input=question,
                    tenant_id="cerai-demo",
                    user_id="demo-learner",
                )
            ),
        )
    finally:
        await container.aclose()


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Responsible-AI virtual teaching assistant")
    parser.add_argument(
        "question",
        nargs="?",
        default="Explain why model accuracy alone is not enough for responsible AI.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use the deterministic mock provider with a scripted answer; needs no API key.",
    )
    return parser.parse_args(arguments)


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parse_args(argv)
    if arguments.offline:
        print("[offline: scripted mock tutor; retrieval, guardrails and citations are real]")
    else:
        require_key("OPENAI_API_KEY")
    result = asyncio.run(ask(arguments.question, offline=arguments.offline))
    if result.error:
        raise RuntimeError(result.error)
    print(result.output or "")


if __name__ == "__main__":
    main()

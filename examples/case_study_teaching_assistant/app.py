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
from examples.case_study_teaching_assistant.application import register_teaching_tools

EXAMPLE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = EXAMPLE_DIR / "config" / "agent.yaml"
UI_PATH = EXAMPLE_DIR / "ui" / "index.html"


async def ask(question: str) -> RunResult:
    container = build_container(load_settings((CONFIG_PATH,)))
    register_teaching_tools(container.tools)
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
    return parser.parse_args(arguments)


def main() -> None:
    result = asyncio.run(ask(parse_args().question))
    if result.error:
        raise RuntimeError(result.error)
    print(result.output or "")


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.types.contracts import RunRequest


async def chat(message: str) -> str:
    container = build_container(load_settings((Path(__file__).with_name("agent.yaml"),)))
    try:
        result = await AlgenAgentRuntimeClient(container.runtime).run(
            RunRequest(agent="cloud-chat", input=message, tenant_id="quickstart", user_id="user")
        )
        if result.error:
            raise RuntimeError(result.error)
        return result.output or ""
    finally:
        container.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the cloud-chat quickstart")
    parser.add_argument("message", nargs="?", default="Explain governed agents in one sentence.")
    print(asyncio.run(chat(parser.parse_args().message)))


if __name__ == "__main__":
    main()

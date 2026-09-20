from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from examples.workflow_cli import print_result, run_example


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the bounded multi-agent fan-out example")
    parser.add_argument("question", nargs="?", default="Assess a migration to managed queues")
    args = parser.parse_args()
    values = asyncio.run(
        run_example(Path(__file__).with_name("agent.yaml"), "multi-agent-fanout", args.question)
    )
    print_result(values)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from examples.workflow_cli import print_result, run_example


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the human-checkpoint quickstart")
    parser.add_argument("change", nargs="?", default="Enable the synthetic customer credit")
    values = asyncio.run(
        run_example(
            Path(__file__).with_name("agent.yaml"),
            "approval-quickstart",
            parser.parse_args().change,
        )
    )
    print_result(values)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from examples.workflow_cli import (
    add_decision_arguments,
    decision_provider_from_args,
    print_result,
    run_example,
)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the human-checkpoint quickstart")
    parser.add_argument("change", nargs="?", default="Enable the synthetic customer credit")
    add_decision_arguments(parser)
    args = parser.parse_args(argv)
    values = asyncio.run(
        run_example(
            Path(__file__).with_name("agent.yaml"),
            "approval-quickstart",
            args.change,
            decision_provider=decision_provider_from_args(args),
        )
    )
    print_result(values)


if __name__ == "__main__":
    main()

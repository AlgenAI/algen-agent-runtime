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
    parser = argparse.ArgumentParser(description="Run the typed read-only capability quickstart")
    parser.add_argument("question", nargs="?", default="How many units of SKU-100 are available?")
    add_decision_arguments(parser)
    args = parser.parse_args(argv)
    values = asyncio.run(
        run_example(
            Path(__file__).with_name("agent.yaml"),
            "tool-quickstart",
            args.question,
            decision_provider=decision_provider_from_args(args),
        )
    )
    print_result(values)


if __name__ == "__main__":
    main()

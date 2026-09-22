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
    parser = argparse.ArgumentParser(description="Run synthetic customer-support resolution")
    parser.add_argument("case", nargs="?", default="Please refund my order")
    add_decision_arguments(parser)
    args = parser.parse_args(argv)
    values = asyncio.run(
        run_example(
            Path(__file__).parent / "config" / "agent.yaml",
            "customer-support-resolution",
            args.case,
            decision_provider=decision_provider_from_args(args),
        )
    )
    print_result(values)


if __name__ == "__main__":
    main()

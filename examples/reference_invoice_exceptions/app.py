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
    parser = argparse.ArgumentParser(description="Run synthetic invoice exception review")
    parser.add_argument("document", nargs="?", default="Review invoice INV-100")
    add_decision_arguments(parser)
    args = parser.parse_args(argv)
    values = asyncio.run(
        run_example(
            Path(__file__).parent / "config" / "agent.yaml",
            "invoice-exception-review",
            args.document,
            decision_provider=decision_provider_from_args(args),
        )
    )
    print_result(values)


if __name__ == "__main__":
    main()

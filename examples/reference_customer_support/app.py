from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from examples.workflow_cli import print_result, run_example


def main() -> None:
    parser = argparse.ArgumentParser(description="Run synthetic customer-support resolution")
    parser.add_argument("case", nargs="?", default="Please refund my order")
    values = asyncio.run(
        run_example(
            Path(__file__).parent / "config" / "agent.yaml",
            "customer-support-resolution",
            parser.parse_args().case,
        )
    )
    print_result(values)


if __name__ == "__main__":
    main()

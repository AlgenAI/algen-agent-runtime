from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from examples.workflow_cli import print_result, run_example


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the typed read-only capability quickstart")
    parser.add_argument("question", nargs="?", default="How many units of SKU-100 are available?")
    values = asyncio.run(
        run_example(
            Path(__file__).with_name("agent.yaml"), "tool-quickstart", parser.parse_args().question
        )
    )
    print_result(values)


if __name__ == "__main__":
    main()

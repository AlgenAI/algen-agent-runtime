from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from examples.workflow_cli import print_result, run_example


def main() -> None:
    parser = argparse.ArgumentParser(description="Run governed local-evidence research")
    parser.add_argument("question", nargs="?", default="Who owns workflow behavior?")
    values = asyncio.run(
        run_example(
            Path(__file__).with_name("agent.yaml"),
            "governed-research",
            parser.parse_args().question,
        )
    )
    print_result(values)


if __name__ == "__main__":
    main()

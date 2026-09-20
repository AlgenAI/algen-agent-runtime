from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from examples.workflow_cli import print_result, run_example


def main() -> None:
    parser = argparse.ArgumentParser(description="Run synthetic incident response")
    parser.add_argument("alert", nargs="?", default="Checkout error rate is above threshold")
    values = asyncio.run(
        run_example(
            Path(__file__).parent / "config" / "agent.yaml",
            "incident-response",
            parser.parse_args().alert,
        )
    )
    print_result(values)


if __name__ == "__main__":
    main()

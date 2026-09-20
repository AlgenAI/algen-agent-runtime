from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from examples.workflow_cli import print_result, run_example


def main() -> None:
    parser = argparse.ArgumentParser(description="Run synthetic invoice exception review")
    parser.add_argument("document", nargs="?", default="Review invoice INV-100")
    values = asyncio.run(
        run_example(
            Path(__file__).parent / "config" / "agent.yaml",
            "invoice-exception-review",
            parser.parse_args().document,
        )
    )
    print_result(values)


if __name__ == "__main__":
    main()

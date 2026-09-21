from __future__ import annotations

import asyncio
from pathlib import Path

from examples.workflow_cli import print_result, run_example


def main() -> None:
    values = asyncio.run(
        run_example(
            Path(__file__).with_name("agent.yaml"),
            "composed-parent",
            "Review candidate batch 42",
        )
    )
    print_result(values)


if __name__ == "__main__":
    main()

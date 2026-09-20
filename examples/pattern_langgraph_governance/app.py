from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from examples.workflow_cli import print_result, run_example


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a graph through Runtime's LangGraph adapter")
    parser.add_argument("question", nargs="?", default="Check framework identity propagation")
    values = asyncio.run(
        run_example(
            Path(__file__).with_name("agent.yaml"),
            "langgraph-governance",
            parser.parse_args().question,
        )
    )
    print_result(values)


if __name__ == "__main__":
    main()

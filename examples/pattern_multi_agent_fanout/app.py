from __future__ import annotations

import argparse
from pathlib import Path

from examples.workflow_cli import (
    add_decision_arguments,
    decision_provider_from_args,
    print_result,
    run_example_cli,
)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the bounded multi-agent fan-out example")
    parser.add_argument("question", nargs="?", default="Assess a migration to managed queues")
    add_decision_arguments(parser)
    args = parser.parse_args(argv)
    values = run_example_cli(
        Path(__file__).with_name("agent.yaml"),
        "multi-agent-fanout",
        args.question,
        decision_provider=decision_provider_from_args(args),
    )
    print_result(values)


if __name__ == "__main__":
    main()

from __future__ import annotations

from pathlib import Path

EXAMPLE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = EXAMPLE_DIR / "config" / "agent.yaml"
UI_PATH = EXAMPLE_DIR / "ui" / "index.html"
MIGRATION_PATH = EXAMPLE_DIR / "database" / "migrations" / "001_hiring_schema.sql"
TEMPLATE_CORPUS_PATH = EXAMPLE_DIR / "data" / "template_corpus.md"
TENANT_ID = "hiring-example"


def main() -> None:
    from examples.case_study_responsible_hiring.dashboard import main as dashboard_main

    dashboard_main()


if __name__ == "__main__":
    main()

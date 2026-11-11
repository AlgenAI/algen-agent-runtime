"""examples/README.md lists every example (#98)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
HELPERS = {"__init__.py", "offline.py", "workflow_cli.py", "plugins.py"}


def test_every_example_has_a_row_in_the_index() -> None:
    index = (EXAMPLES / "README.md").read_text()
    entries = sorted(
        path.name
        for path in EXAMPLES.iterdir()
        if (path.is_dir() and (path / "__init__.py").is_file())
        or (path.suffix == ".py" and path.name not in HELPERS)
    )

    missing = [name for name in entries if f"[`{name}`]({name})" not in index]

    assert entries
    assert missing == []

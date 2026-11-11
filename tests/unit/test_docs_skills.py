"""docs/docs.html shows the current Agent Skills for the newest release line."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _sync_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "sync_docs_skills", ROOT / "scripts/sync_docs_skills.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_docs_page_has_the_current_skills() -> None:
    sync = _sync_module()
    page = (ROOT / "docs/docs.html").read_text()
    first, last, ref = sync.LINES[-1]
    assert last is None and ref is None, "the newest line must read the working tree"

    stale = [
        folder
        for folder in sync.SKILLS.values()
        if f'<code data-version-min="{first}">{sync._render(sync._source(folder, None))}</code>'
        not in page
    ]

    assert stale == [], "run: python scripts/sync_docs_skills.py"

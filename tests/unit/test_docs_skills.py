"""docs/docs.html shows the current Agent Skills for the newest release line."""

from __future__ import annotations

import importlib.util
import re
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
    _, last, ref = sync.LINES[-1]
    assert last is None and ref is None, "the newest line must read the working tree"

    stale = []
    for file_id, folder in sync.SKILLS.items():
        first = sync.gated_lines(file_id)[-1][0]
        current = sync._render(sync._source(folder, None))
        if f'<code data-version-min="{first}">{current}</code>' not in page:
            stale.append(folder)

    assert stale == [], "run: python scripts/sync_docs_skills.py"


def test_a_skill_added_later_is_shown_only_from_its_first_release() -> None:
    sync = _sync_module()
    page = (ROOT / "docs/docs.html").read_text()

    for file_id, since in sync.SINCE.items():
        assert sync.gated_lines(file_id)[0][0] == since
        # Its card, explorer entry and tab are gated like its text.
        tags = re.findall(rf'<(?!pre)[a-z]+ [^>]*data-(?:open-skill|file)="{file_id}"[^>]*>', page)
        assert tags, file_id
        for tag in tags:
            assert f'data-version-min="{since}"' in tag, tag

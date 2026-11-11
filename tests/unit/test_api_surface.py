"""The public surface matches docs/api/surface.json, so no change to it goes unannounced.

A failure lists what changed. If the change is intended, run
``python scripts/api_surface.py --update``, commit docs/api/surface.json with the change, and record
it in CHANGELOG.md; a removal or an incompatible change needs an upgrade note.
"""

from __future__ import annotations

import difflib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("api_surface", ROOT / "scripts/api_surface.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_public_surface_matches_the_baseline() -> None:
    surface = _module()
    current = surface.render(surface.surface())
    baseline = surface.BASELINE.read_text(encoding="utf-8")

    if current != baseline:
        diff = "".join(
            difflib.unified_diff(
                baseline.splitlines(keepends=True),
                current.splitlines(keepends=True),
                "docs/api/surface.json",
                "current code",
                n=1,
            )
        )
        raise AssertionError(
            "the public surface changed; if intended, run "
            "`python scripts/api_surface.py --update` and record it in CHANGELOG.md:\n" + diff
        )
    assert json.loads(baseline)["exports"]

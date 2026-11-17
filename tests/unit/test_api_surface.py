"""The public surface matches docs/api/surface.json, so no change to it goes unannounced.

A failure lists what changed, grouped: removed (breaking), changed (check compatibility), added,
and experimental (allowed with a CHANGELOG line, no deprecation window). If the change is
intended, run ``python scripts/api_surface.py --update``, commit docs/api/surface.json with the
change, and record it in CHANGELOG.md; a removal or an incompatible change needs an upgrade note.
"""

from __future__ import annotations

import difflib
import importlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("api_surface", ROOT / "scripts/api_surface.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def surface_module():  # type: ignore[no-untyped-def]
    return _module()


@pytest.fixture(scope="module")
def current(surface_module) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return surface_module.surface()  # type: ignore[no-any-return]


def test_the_public_surface_matches_the_baseline(surface_module, current) -> None:  # type: ignore[no-untyped-def]
    rendered = surface_module.render(current)
    baseline = surface_module.BASELINE.read_text(encoding="utf-8")

    if rendered != baseline:
        grouped = surface_module.describe_changes(
            surface_module.changes(json.loads(baseline), current)
        )
        diff = "".join(
            difflib.unified_diff(
                baseline.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                "docs/api/surface.json",
                "current code",
                n=1,
            )
        )
        raise AssertionError(
            "the public surface changed; if intended, run "
            "`python scripts/api_surface.py --update` and record it in CHANGELOG.md:\n"
            + grouped
            + "\n\n"
            + diff
        )
    assert json.loads(baseline)["exports"]


def _surface(**sections: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "exports": {"pkg": {"Stable": {"class": "()"}}},
        "configuration": {"stable_field": "integer"},
        "events": ["run.started"],
        "event_payloads": {"run": {"run.started": ["agent"]}},
        "experimental": {"configuration": [], "exports": [], "protocols": []},
    }
    base.update(sections)
    return base


def test_changes_group_stable_and_experimental_entries(surface_module) -> None:  # type: ignore[no-untyped-def]
    baseline = _surface(
        exports={
            "pkg": {
                "Stable": {"class": "()"},
                "Trial": {"class": "()", "stability": "experimental"},
            }
        },
        configuration={"stable_field": "integer", "trial": "object"},
        experimental={"configuration": ["trial"], "exports": ["pkg.Trial"], "protocols": []},
    )
    current = _surface(
        exports={"pkg": {"Stable": {"class": "(x)"}, "New": {"class": "()"}}},
        configuration={"stable_field": "integer"},
        events=[],
        event_payloads={"run": {"run.started": ["agent", "model"]}},
    )

    grouped = surface_module.changes(baseline, current)

    # A change to an experimental entry, even its removal, is not breaking.
    assert grouped["experimental"] == ["configuration/trial", "exports/pkg.Trial"]
    assert grouped["removed"] == ["events/run.started"]
    assert grouped["changed"] == ["exports/pkg.Stable"]
    # A payload that only gains keys is additive.
    assert grouped["added"] == ["event_payloads/run/run.started", "exports/pkg.New"]


def test_a_stable_entry_made_experimental_is_a_change(surface_module) -> None:  # type: ignore[no-untyped-def]
    baseline = _surface()
    current = _surface(
        exports={"pkg": {"Stable": {"class": "()", "stability": "experimental"}}},
        experimental={"configuration": [], "exports": ["pkg.Stable"], "protocols": []},
    )

    grouped = surface_module.changes(baseline, current)

    assert grouped["changed"] == ["exports/pkg.Stable"]
    assert not grouped["experimental"]


def test_experimental_entries_are_marked_in_docstrings_and_docs(current) -> None:  # type: ignore[no-untyped-def]
    from algen_agent_runtime.stability import experimental_reason

    docs = (ROOT / "docs" / "api-stability.md").read_text(encoding="utf-8")
    index = current["experimental"]
    assert index["exports"], "the experimental interfaces are marked in code"
    objects: dict[str, Any] = {}
    for qualified in index["exports"] + index["protocols"]:
        module_name, name = qualified.rsplit(".", 1)
        objects[qualified] = getattr(importlib.import_module(module_name), name)

    for qualified, value in objects.items():
        name = qualified.rsplit(".", 1)[1]
        assert experimental_reason(value), qualified
        assert (value.__doc__ or "").startswith("Experimental: "), (
            f"{qualified}'s docstring starts with 'Experimental: '"
        )
        assert f"`{name}`" in docs, f"docs/api-stability.md names {name}"
    configuration = set(index["configuration"])
    for path in configuration:
        parent = path.rsplit(".", 1)[0] if "." in path else None
        if parent not in configuration:
            assert f"`{path}`" in docs, f"docs/api-stability.md names the {path} setting"


def test_event_payloads_record_the_public_events(current) -> None:  # type: ignore[no-untyped-def]
    payloads = current["event_payloads"]

    assert {"run", "conversation", "workflow", "envelopes", "sse"} <= payloads.keys()
    assert payloads["run"]["run.started"] == ["agent"]
    assert "sequence" in payloads["envelopes"]["run"]
    assert "workflow_id" in payloads["envelopes"]["workflow"]
    assert payloads["sse"]["_sse"].startswith("id: {event.sequence}\nevent: {event.type}\n")
    # Every payload's event type is one the surface lists.
    events = set(current["events"])
    for family in ("run", "conversation", "workflow"):
        assert set(payloads[family]) <= events, family

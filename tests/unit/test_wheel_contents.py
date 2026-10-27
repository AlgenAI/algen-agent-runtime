"""The wheel ships the generic examples Studio imports, but no domain case studies."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOMAIN_EXAMPLES = ("case_study_responsible_hiring", "pattern_text_to_sql")


def _wheel_target() -> dict[str, object]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    target: dict[str, object] = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    return target


def test_the_wheel_packages_examples_so_exclude_applies() -> None:
    target = _wheel_target()

    assert "examples" in target["packages"]  # type: ignore[operator]
    # force-include bypasses exclude, so it must not bring examples back.
    assert "examples" not in dict(target.get("force-include", {}))  # type: ignore[call-overload]


def test_domain_case_studies_stay_out_of_the_wheel() -> None:
    excluded = set(_wheel_target()["exclude"])  # type: ignore[call-overload]

    for name in DOMAIN_EXAMPLES:
        assert (ROOT / "examples" / name).is_dir(), name
        assert f"examples/{name}" in excluded, name

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

    assert "algen_agent_runtime_examples" in target["packages"]  # type: ignore[operator]
    # force-include bypasses exclude, so it must not bring examples back.
    assert "algen_agent_runtime_examples" not in dict(target.get("force-include", {}))  # type: ignore[call-overload]


def test_domain_case_studies_stay_out_of_the_wheel() -> None:
    excluded = set(_wheel_target()["exclude"])  # type: ignore[call-overload]

    for name in DOMAIN_EXAMPLES:
        assert (ROOT / "algen_agent_runtime_examples" / name).is_dir(), name
        assert f"algen_agent_runtime_examples/{name}" in excluded, name


def test_the_sdist_includes_every_wheel_package() -> None:
    # CI builds the wheel from the sdist, so a package the sdist leaves out is missing from the
    # wheel too, although a wheel built from the checkout has it.
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    included = {
        path.strip("/") for path in config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    }

    for package in _wheel_target()["packages"]:  # type: ignore[attr-defined]
        assert package in included, package

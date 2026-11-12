"""Check the built distributions: what the wheel and sdist must and must not contain.

CI's package job and the release workflow run this on `dist/`, and so does
scripts/ci-local.sh. Every SQL migration in the source tree must be in the wheel, so a new
migration is checked without editing this file. So must every file of each Agent Skill that
pyproject.toml force-includes (under algen_agent_runtime/_skills), and no other skill.

Usage: python scripts/check_dist.py [DIST_DIR]
"""

from __future__ import annotations

import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "algen_agent_runtime/py.typed",
    "algen_agent_runtime/persistence/schema_catalog.json",
    "algen_agent_runtime/context/compaction.py",
    "algen_agent_runtime/context/working_state.py",
    "algen_agent_runtime/context/skills.py",
    "algen_agent_runtime/memory/service.py",
    "algen_agent_runtime/evaluation/context.py",
    "algen_agent_runtime/tools/starter/__init__.py",
    "algen_agent_runtime/mcp/__init__.py",
    "algen_agent_runtime_examples/context_evaluation.py",
    "algen_agent_runtime_examples/quickstart_approval/__init__.py",
    "algen_agent_runtime_examples/quickstart_conversation/agent.yaml",
    "algen_agent_runtime_examples/pattern_guarded_tools/agent.yaml",
    "algen_agent_runtime_examples/pattern_side_effect_reconciliation/agent.yaml",
    "algen_agent_runtime_examples/pattern_secured_api/agent.yaml",
}
DOMAIN = (
    "algen_agent_runtime_examples/case_study_responsible_hiring/",
    "algen_agent_runtime_examples/pattern_text_to_sql/",
)
SDIST_REQUIRED = ("LICENSE", "NOTICE", "README.md", "CHANGELOG.md", "CHANGELOG-0.1.md")
SKILLS_PREFIX = "algen_agent_runtime/_skills/"
# Skills about working on the Runtime repository itself; never shipped in the wheel.
MAINTAINER_SKILLS = ("algen-external-pr-review", "algen-runtime-contributor")


def packaged_skills() -> dict[str, str]:
    """Source folder -> wheel folder of each skill pyproject.toml force-includes."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    include = config["tool"]["hatch"]["build"]["targets"]["wheel"].get("force-include", {})
    return {
        source: target for source, target in include.items() if target.startswith(SKILLS_PREFIX)
    }


def skill_files() -> tuple[set[str], set[str]]:
    """The files the wheel and the sdist must carry for the packaged skills."""
    wheel: set[str] = set()
    sdist: set[str] = set()
    for source, target in packaged_skills().items():
        folder = ROOT / source
        files = [path for path in folder.rglob("*") if path.is_file()]
        if not (folder / "SKILL.md").is_file():
            raise SystemExit(f"{source} has no SKILL.md")
        for path in files:
            relative = path.relative_to(folder).as_posix()
            wheel.add(f"{target}/{relative}")
            sdist.add(f"{source}/{relative}")
    return wheel, sdist


def main(dist: Path) -> None:
    wheel = next(dist.glob("*.whl"))
    sdist = next(dist.glob("*.tar.gz"))
    migrations = {
        f"algen_agent_runtime/persistence/migrations/{path.name}"
        for path in (ROOT / "src/algen_agent_runtime/persistence/migrations").glob("*.sql")
    }
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    wheel_skills, sdist_skills = skill_files()
    if not wheel_skills:
        raise SystemExit("pyproject.toml force-includes no Agent Skills")
    missing = sorted((REQUIRED | migrations | wheel_skills) - names)
    if missing:
        raise SystemExit(f"wheel missing required files: {missing}")
    extra_skills = sorted(
        name for name in names if name.startswith(SKILLS_PREFIX) and name not in wheel_skills
    )
    if extra_skills:
        raise SystemExit(f"wheel has skill files the source tree does not: {extra_skills}")
    if any(
        name.startswith(f"{SKILLS_PREFIX}{skill}/") for name in names for skill in MAINTAINER_SKILLS
    ):
        raise SystemExit("wheel contains a maintainer-only skill")
    if any("studio" in name.lower() for name in names):
        raise SystemExit("wheel unexpectedly contains Studio files")
    if any(name.startswith(DOMAIN) for name in names):
        raise SystemExit(
            "wheel contains domain case studies (see tests/unit/test_wheel_contents.py)"
        )
    if any("__pycache__" in name or name.endswith(".pyc") for name in names):
        raise SystemExit("wheel contains Python cache files")
    with tarfile.open(sdist) as archive:
        sdist_names = archive.getnames()
    for required in SDIST_REQUIRED:
        if not any(name.endswith(f"/{required}") for name in sdist_names):
            raise SystemExit(f"sdist missing {required}")
    # The sdist's top folder is <name>-<version>/; the wheel is built from it.
    sdist_relative = {name.partition("/")[2] for name in sdist_names}
    missing_skills = sorted(sdist_skills - sdist_relative)
    if missing_skills:
        raise SystemExit(f"sdist missing skill files: {missing_skills}")
    skills = len(packaged_skills())
    print(
        f"{wheel.name}: {len(names)} files, {len(migrations)} migrations, {skills} skills; "
        f"{sdist.name}: OK"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "dist"))

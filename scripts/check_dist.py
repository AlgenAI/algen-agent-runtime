"""Check the built distributions: what the wheel and sdist must and must not contain.

CI's package job and the release workflow run this on `dist/`, and so does
scripts/ci-local.sh. Every SQL migration in the source tree must be in the wheel, so a new
migration is checked without editing this file.

Usage: python scripts/check_dist.py [DIST_DIR]
"""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "algen_agent_runtime/py.typed",
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


def main(dist: Path) -> None:
    wheel = next(dist.glob("*.whl"))
    sdist = next(dist.glob("*.tar.gz"))
    migrations = {
        f"algen_agent_runtime/persistence/migrations/{path.name}"
        for path in (ROOT / "src/algen_agent_runtime/persistence/migrations").glob("*.sql")
    }
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    missing = sorted((REQUIRED | migrations) - names)
    if missing:
        raise SystemExit(f"wheel missing required files: {missing}")
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
    print(f"{wheel.name}: {len(names)} files, {len(migrations)} migrations; {sdist.name}: OK")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "dist"))

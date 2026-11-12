"""The Agent Skills shipped in the package, for ``algen-agent-runtime skills``.

The skills live once, in the repository's ``.agents/skills``. The wheel carries the user-facing
ones as ``algen_agent_runtime/_skills/<name>`` (``force-include`` in ``pyproject.toml``); a source
checkout or an editable install reads them from ``.agents/skills`` instead. The maintainer-only
skills (``algen-external-pr-review``, ``algen-runtime-contributor``) are about working on the
Runtime repository itself and are not shipped.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

# Must match the `_skills` entries of [tool.hatch.build.targets.wheel.force-include]
# (tests/unit/test_coding_skills.py checks it).
PACKAGED_SKILLS = (
    "algen-agent-builder",
    "algen-agent-connector",
    "algen-runtime-extension",
    "algen-runtime-host",
)
DEFAULT_SKILLS_DIR = Path(".agents") / "skills"


@dataclass(frozen=True)
class CodingSkill:
    name: str
    description: str
    source: Traversable


def skills_root() -> Traversable:
    """Where this installation's skills are: the wheel's copy, or a source checkout's."""
    packaged = files("algen_agent_runtime").joinpath("_skills")
    if packaged.is_dir():
        return packaged
    checkout = Path(__file__).resolve().parents[2] / ".agents" / "skills"
    if checkout.is_dir():
        return checkout
    raise FileNotFoundError("this installation of algen-agent-runtime has no packaged skills")


def _front_matter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    values: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = value.strip()
    return values


def available_skills() -> tuple[CodingSkill, ...]:
    root = skills_root()
    found = []
    for name in PACKAGED_SKILLS:
        source = root.joinpath(name)
        manifest = source.joinpath("SKILL.md")
        if not manifest.is_file():
            raise FileNotFoundError(f"packaged skill {name!r} has no SKILL.md")
        meta = _front_matter(manifest.read_text(encoding="utf-8"))
        found.append(CodingSkill(name, meta.get("description", ""), source))
    return tuple(found)


def _files(source: Traversable, prefix: Path = Path()) -> Iterator[tuple[Path, Traversable]]:
    for item in sorted(source.iterdir(), key=lambda entry: entry.name):
        if item.name == "__pycache__" or item.name.startswith("."):
            continue
        if item.is_dir():
            yield from _files(item, prefix / item.name)
        else:
            yield prefix / item.name, item


def install_skills(
    project: Path,
    names: Sequence[str] = (),
    *,
    skills_dir: Path = DEFAULT_SKILLS_DIR,
    force: bool = False,
) -> list[Path]:
    """Copy the named skills (all when empty) into ``project / skills_dir`` and return the
    files written.

    Raises ``ValueError`` for an unknown name and ``FileExistsError`` when a skill's folder is
    already there and ``force`` is false; nothing is written then. With ``force`` each skill's
    folder is replaced, so files a newer version dropped do not linger.
    """
    skills = {skill.name: skill for skill in available_skills()}
    unknown = [name for name in names if name not in skills]
    if unknown:
        raise ValueError(
            f"unknown skill(s) {', '.join(unknown)}; available: {', '.join(PACKAGED_SKILLS)}"
        )
    selected = [skills[name] for name in dict.fromkeys(names)] if names else list(skills.values())
    target = project / skills_dir
    existing = [str(target / skill.name) for skill in selected if (target / skill.name).exists()]
    if existing and not force:
        raise FileExistsError(
            f"already installed: {', '.join(existing)} (pass --force to replace them)"
        )
    written: list[Path] = []
    for skill in selected:
        destination = target / skill.name
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        elif destination.exists() or destination.is_symlink():
            destination.unlink()
        for relative, item in _files(skill.source):
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(item.read_bytes())
            written.append(path)
    return written

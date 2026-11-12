"""`algen-agent-runtime skills`: the Agent Skills shipped in the package."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pytest

from algen_agent_runtime import _coding_skills
from algen_agent_runtime._coding_skills import PACKAGED_SKILLS, available_skills, install_skills
from algen_agent_runtime.cli import main

ROOT = Path(__file__).resolve().parents[2]
MAINTAINER_SKILLS = ("algen-external-pr-review", "algen-runtime-contributor")


def _skills(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as exit_info:
        main(["skills", *argv])
    captured = capsys.readouterr()
    return int(exit_info.value.code or 0), captured.out, captured.err


def _source_files(name: str) -> set[str]:
    folder = ROOT / ".agents/skills" / name
    return {path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()}


def test_the_wheel_force_includes_exactly_the_packaged_skills() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    include = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    skills = {
        source: target
        for source, target in include.items()
        if target.startswith("algen_agent_runtime/_skills/")
    }

    assert skills == {
        f".agents/skills/{name}": f"algen_agent_runtime/_skills/{name}" for name in PACKAGED_SKILLS
    }
    # The sdist carries the folders, so a wheel built from it has them.
    assert "/.agents/skills" in config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]


def test_maintainer_skills_are_not_packaged() -> None:
    for name in MAINTAINER_SKILLS:
        assert (ROOT / ".agents/skills" / name / "SKILL.md").is_file(), name
        assert name not in PACKAGED_SKILLS


def test_every_repository_skill_is_packaged_or_maintainer_only() -> None:
    folders = {path.parent.name for path in (ROOT / ".agents/skills").glob("*/SKILL.md")}

    assert folders == set(PACKAGED_SKILLS) | set(MAINTAINER_SKILLS)


def test_packaged_skills_are_on_the_docs_page() -> None:
    spec = importlib.util.spec_from_file_location("sync", ROOT / "scripts/sync_docs_skills.py")
    assert spec and spec.loader
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)

    assert set(PACKAGED_SKILLS) <= set(sync.SKILLS.values())


def test_each_skill_names_itself_and_says_what_it_is_for() -> None:
    skills = available_skills()

    assert [skill.name for skill in skills] == list(PACKAGED_SKILLS)
    for skill in skills:
        text = skill.source.joinpath("SKILL.md").read_text(encoding="utf-8")
        assert _coding_skills._front_matter(text)["name"] == skill.name
        assert skill.description


def test_list_prints_each_skill(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = _skills(capsys, "list")

    assert code == 0
    assert [line.split(":", 1)[0] for line in out.splitlines()] == list(PACKAGED_SKILLS)
    assert "algen-runtime-contributor" not in out


def test_install_copies_every_file_and_prints_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _skills(capsys, "install", "--path", str(tmp_path))

    assert code == 0
    target = tmp_path / ".agents/skills"
    for name in PACKAGED_SKILLS:
        installed = {
            path.relative_to(target / name).as_posix()
            for path in (target / name).rglob("*")
            if path.is_file()
        }
        assert installed == _source_files(name)
        assert "references" in {Path(item).parts[0] for item in installed}
        assert (target / name / "SKILL.md").read_bytes() == (
            ROOT / ".agents/skills" / name / "SKILL.md"
        ).read_bytes()
        assert f"wrote {target / name / 'SKILL.md'}" in out
    assert out.rstrip().endswith(", ".join(PACKAGED_SKILLS))


def test_install_one_skill_into_another_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _skills(
        capsys,
        "install",
        "--path",
        str(tmp_path),
        "--skill",
        "algen-runtime-host",
        "--skills-dir",
        ".claude/skills",
    )

    assert code == 0
    assert [path.name for path in (tmp_path / ".claude/skills").iterdir()] == ["algen-runtime-host"]
    assert (
        tmp_path / ".claude/skills/algen-runtime-host/references/host-capabilities.md"
    ).is_file()
    assert "installed 1 skill(s)" in out


def test_install_refuses_to_overwrite_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    host = tmp_path / ".agents/skills/algen-runtime-host"
    host.mkdir(parents=True)
    (host / "SKILL.md").write_text("my edits")

    code, out, err = _skills(capsys, "install", "--path", str(tmp_path))

    assert code == 1
    assert "already installed" in err and "--force" in err
    assert out == ""
    # Nothing is written, not even the skills that were not there yet.
    assert sorted(path.name for path in host.parent.iterdir()) == ["algen-runtime-host"]
    assert (host / "SKILL.md").read_text() == "my edits"


def test_force_replaces_the_folder(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    host = tmp_path / ".agents/skills/algen-runtime-host"
    host.mkdir(parents=True)
    (host / "stale.md").write_text("from an older version")

    code, _, _ = _skills(
        capsys, "install", "--path", str(tmp_path), "--skill", "algen-runtime-host", "--force"
    )

    assert code == 0
    assert not (host / "stale.md").exists()
    assert (host / "SKILL.md").is_file()


def test_unknown_skill_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = _skills(
        capsys, "install", "--path", str(tmp_path), "--skill", "algen-runtime-contributor"
    )

    assert code == 1
    assert "unknown skill(s) algen-runtime-contributor" in err
    assert not (tmp_path / ".agents").exists()


def test_install_reads_the_packaged_copy_when_there_is_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaged = tmp_path / "_skills"
    for name in PACKAGED_SKILLS:
        (packaged / name / "references").mkdir(parents=True)
        (packaged / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: packaged\n---\n"
        )
        (packaged / name / "references" / "notes.md").write_text("packaged")
    monkeypatch.setattr(_coding_skills, "skills_root", lambda: packaged)

    written = install_skills(tmp_path / "project", ["algen-agent-builder"])

    assert {path.name for path in written} == {"SKILL.md", "notes.md"}
    assert available_skills()[0].description == "packaged"

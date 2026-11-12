"""Copy the Agent Skills (.agents/skills/*/SKILL.md) into docs/docs.html, one copy per release line.

The docs page shows each skill in its "Skills & prompts" editor. A reader who selects an older
release must see the skill as it was for that release, so each skill's <pre> holds one <code> per
release line, gated with data-version-min / data-version-max like the rest of the page.

Usage:
    python scripts/sync_docs_skills.py          # rewrite docs/docs.html
    python scripts/sync_docs_skills.py --check  # exit 1 if docs/docs.html is out of date

When a new release line starts (for example 0.3), freeze the current line by adding its last
release and tag to LINES below, then point the open-ended entry at the new line's first release.
"""

from __future__ import annotations

import argparse
import html
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "docs.html"

# docs.html file id -> skill folder. The external-PR review skill is for maintainers only.
SKILLS = {
    "sk-builder": "algen-agent-builder",
    "sk-connector": "algen-agent-connector",
    "sk-extension": "algen-runtime-extension",
    "sk-contributor": "algen-runtime-contributor",
    "sk-host": "algen-runtime-host",
}

# Skills added after the start of a release line: file id -> the first release that has them. The
# page shows such a skill from that release on, and release lines that ended before it was added
# get no copy (the skill is not in their tag).
SINCE = {
    "sk-host": "0.2.0b3",
}

# (first release, last release, git ref). A ref of None reads the working tree.
LINES: list[tuple[str | None, str | None, str | None]] = [
    (None, "0.1.0a23", "v0.1.0a23"),
    ("0.2.0b1", None, None),
]


def _source(folder: str, ref: str | None) -> str:
    path = f".agents/skills/{folder}/SKILL.md"
    if ref is None:
        return (ROOT / path).read_text()
    return subprocess.run(
        ["git", "show", f"{ref}:{path}"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def _render(markdown: str) -> str:
    """The page's markup: front-matter rules and keys, headings, everything else as text."""
    lines = markdown.rstrip("\n").split("\n")
    out: list[str] = []
    in_front_matter = False
    for number, line in enumerate(lines):
        text = html.escape(line, quote=False)
        if line == "---" and (number == 0 or in_front_matter):
            in_front_matter = number == 0
            out.append('<span class="t-c">---</span>')
        elif in_front_matter and (match := re.match(r"^([A-Za-z_-]+):(.*)$", line)):
            out.append(
                f'<span class="t-y">{match.group(1)}</span>:{html.escape(match.group(2), quote=False)}'
            )
        elif line.startswith("#"):
            out.append(f'<span class="t-k">{text}</span>')
        else:
            out.append(text)
    return "\n".join(out)


def _version_key(version: str) -> tuple[int, int, int, int, int]:
    """Order releases such as 0.1.0a23, 0.2.0b3 and 0.2.0rc1 (final releases last)."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?", version)
    if match is None:
        raise ValueError(f"unexpected release {version!r}")
    major, minor, patch, stage, number = match.groups()
    rank = {"a": 0, "b": 1, "rc": 2, None: 3}[stage]
    return int(major), int(minor), int(patch), rank, int(number or 0)


def gated_lines(file_id: str) -> list[tuple[str | None, str | None, str | None]]:
    """The release lines that show this skill, each as (first release, last release, ref)."""
    since = SINCE.get(file_id)
    if since is None:
        return list(LINES)
    # Without git (CI's base job has none): a line that ended before the skill was added lacks it.
    lines = [
        line for line in LINES if line[1] is None or _version_key(line[1]) >= _version_key(since)
    ]
    if lines and (lines[0][0] is None or _version_key(lines[0][0]) < _version_key(since)):
        lines[0] = (since, *lines[0][1:])
    return lines


def _block(file_id: str) -> str:
    folder = SKILLS[file_id]
    codes = []
    for first, last, ref in gated_lines(file_id):
        gates = "".join(
            f' data-version-{name}="{value}"'
            for name, value in (("min", first), ("max", last))
            if value is not None
        )
        codes.append(f"<code{gates}>{_render(_source(folder, ref))}</code>")
    return "".join(codes)


def sync(page: str) -> str:
    for file_id in SKILLS:
        pattern = re.compile(
            rf'(<pre class="ide-code" data-file="{file_id}"[^>]*>)(.*?)(</pre>)', re.S
        )
        if not pattern.search(page):
            raise SystemExit(f"docs/docs.html has no <pre> for {file_id}")
        block = _block(file_id)
        page = pattern.sub(lambda m, block=block: m.group(1) + block + m.group(3), page)
    return page


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if docs/docs.html is stale")
    args = parser.parse_args()
    current = DOCS.read_text()
    updated = sync(current)
    if args.check:
        if updated != current:
            print("docs/docs.html skills are out of date: run scripts/sync_docs_skills.py")
            sys.exit(1)
        return
    DOCS.write_text(updated)


if __name__ == "__main__":
    main()

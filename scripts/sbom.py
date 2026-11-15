"""Write a CycloneDX SBOM and a license report for an installed environment.

The release workflow installs the wheel with the service extras into a fresh virtual environment
(from the hash-locked ``requirements/service.txt``) and runs this script with that environment's
Python, so the SBOM lists exactly what a deployment of the release installs. It uses only the
standard library, so generating it adds nothing to the release's dependencies.

Usage: python scripts/sbom.py OUT_DIR [--root algen-agent-runtime]

Writes ``OUT_DIR/sbom.cdx.json`` (CycloneDX 1.5 JSON) and ``OUT_DIR/licenses.md``. Exits 1 when a
component's license is unknown or not in ``ALLOWED`` below, so a release cannot ship a dependency
with an unreviewed license.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import uuid
from importlib import metadata
from pathlib import Path

# Licenses the Runtime may depend on (Apache-2.0 compatible). Matched case-insensitively against
# the SPDX expression, the License metadata field and the license classifiers.
ALLOWED = (
    "apache",
    "mit",
    "bsd",
    "isc",
    "psf",
    "python software foundation",
    "mpl",
    "mozilla public license",
    "unlicense",
    "0bsd",
    "zlib",
    "hpnd",
    "historical permission notice",
)
# Packages that are part of the environment, not of the release.
SKIP = {"pip", "setuptools", "wheel"}


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _license(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return expression.strip()
    classifiers = [
        value.split("::")[-1].strip()
        for value in meta.get_all("Classifier") or []
        if value.startswith("License :: OSI Approved ::")
    ]
    if classifiers:
        return " OR ".join(sorted(set(classifiers)))
    text = (meta.get("License") or "").strip()
    first = text.splitlines()[0].strip() if text else ""
    return first[:120] if first else "UNKNOWN"


def _allowed(license_text: str) -> bool:
    lowered = license_text.lower()
    return lowered != "unknown" and any(item in lowered for item in ALLOWED)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--root", default="algen-agent-runtime")
    args = parser.parse_args()

    dists = {}
    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        if not name or _canonical(name) in SKIP:
            continue
        dists[_canonical(name)] = dist
    root_name = _canonical(args.root)
    root = dists.pop(root_name, None)
    if root is None:
        print(f"sbom: {args.root} is not installed in {sys.prefix}", file=sys.stderr)
        return 1

    components = []
    rows = []
    refused = []
    for key in sorted(dists):
        dist = dists[key]
        name, version = dist.metadata["Name"], dist.version
        license_text = _license(dist)
        purl = f"pkg:pypi/{key}@{version}"
        components.append(
            {
                "type": "library",
                "bom-ref": purl,
                "name": name,
                "version": version,
                "purl": purl,
                "licenses": [{"license": {"name": license_text}}],
            }
        )
        rows.append((name, version, license_text))
        if not _allowed(license_text):
            refused.append(f"{name} {version}: {license_text}")

    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
            "tools": {"components": [{"type": "application", "name": "scripts/sbom.py"}]},
            "component": {
                "type": "library",
                "bom-ref": f"pkg:pypi/{root_name}@{root.version}",
                "name": root.metadata["Name"],
                "version": root.version,
                "purl": f"pkg:pypi/{root_name}@{root.version}",
                "licenses": [{"license": {"name": _license(root)}}],
            },
        },
        "components": components,
        "dependencies": [
            {
                "ref": f"pkg:pypi/{root_name}@{root.version}",
                "dependsOn": [component["bom-ref"] for component in components],
            }
        ],
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "sbom.cdx.json").write_text(json.dumps(bom, indent=2) + "\n")
    lines = [
        f"# Licenses: {root.metadata['Name']} {root.version}",
        "",
        f"{root.metadata['Name']} is licensed under {_license(root)}. It installs, with the "
        "service and postgres extras:",
        "",
        "| Package | Version | License |",
        "| --- | --- | --- |",
        *(f"| {name} | {version} | {text.replace('|', '/')} |" for name, version, text in rows),
        "",
    ]
    (args.out_dir / "licenses.md").write_text("\n".join(lines))
    print(f"sbom: {len(components)} components written to {args.out_dir}")
    if refused:
        print("sbom: licenses not in the allowed list:", *refused, sep="\n  ", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

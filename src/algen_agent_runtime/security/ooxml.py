"""Bounded reading of Office Open XML packages (XLSX, PPTX) shared by their inspectors.

The same limits as the DOCX inspector: a part count, an uncompressed size and a compression ratio
cap (zip bombs), XML that never declares a DTD (entity expansion), and findings for macro projects
and relationships that load remote content when the file opens.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree

from algen_agent_runtime.security.documents import DocumentSecurityFinding, FindingSeverity

REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
OFFICE_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
# Relationship types that pull remote content when the file opens; hyperlinks are normal.
_ACTIVE_EXTERNAL = re.compile(r"/(attachedTemplate|oleObject|frame|externalLinkPath)$")


@dataclass(frozen=True)
class PackageLimits:
    max_bytes: int = 10_000_000
    max_members: int = 1_000
    max_uncompressed_bytes: int = 50_000_000
    max_compression_ratio: int = 200


def open_package(data: bytes, kind: str, limits: PackageLimits) -> zipfile.ZipFile:
    """The ZIP package, refused when it is not one or exceeds ``limits``."""
    if len(data) > limits.max_bytes:
        raise ValueError(f"document exceeds {limits.max_bytes} byte limit")
    if not data.startswith(b"PK\x03\x04"):
        raise ValueError(f"file content is not a {kind} package")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"file content is not a {kind} package") from exc
    infos = archive.infolist()
    if len(infos) > limits.max_members:
        archive.close()
        raise ValueError(f"{kind} package has more than {limits.max_members} parts")
    total = 0
    for info in infos:
        total += info.file_size
        ratio = info.file_size / max(1, info.compress_size)
        if total > limits.max_uncompressed_bytes or ratio > limits.max_compression_ratio:
            archive.close()
            raise ValueError(f"{kind} package expands beyond the configured limits")
    return archive


def parse_part(payload: bytes, kind: str) -> ElementTree.Element:
    # Office parts never declare a DTD; refusing one rules out entity-expansion attacks.
    if b"<!DOCTYPE" in payload[:4096].upper() or b"<!ENTITY" in payload.upper():
        raise ValueError(f"{kind} part declares a DTD, which is not allowed")
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise ValueError(f"{kind} part is not well-formed XML") from exc


def relationships(archive: zipfile.ZipFile, rels_name: str, kind: str) -> dict[str, str]:
    """Relationship id to target (as written) for one ``.rels`` part; empty when it is absent."""
    if rels_name not in archive.namelist():
        return {}
    root = parse_part(archive.read(rels_name), kind)
    return {
        relation.get("Id", ""): relation.get("Target", "")
        for relation in root.iter(f"{REL}Relationship")
    }


def package_findings(
    archive: zipfile.ZipFile, folder: str, kind: str
) -> list[DocumentSecurityFinding]:
    """Macro projects and remote content loaded on open, anywhere under ``folder``."""
    findings: list[DocumentSecurityFinding] = []
    names = archive.namelist()
    if any(name.lower().endswith("vbaproject.bin") for name in names):
        findings.append(
            DocumentSecurityFinding(
                kind="document.macros",
                severity=FindingSeverity.CRITICAL,
                message="The document carries a macro project.",
            )
        )
    for name in names:
        if not (name.startswith(folder) and name.endswith(".rels")):
            continue
        for relation in parse_part(archive.read(name), kind).iter(f"{REL}Relationship"):
            if relation.get("TargetMode") == "External" and _ACTIVE_EXTERNAL.search(
                relation.get("Type", "")
            ):
                findings.append(
                    DocumentSecurityFinding(
                        kind="document.external_reference",
                        severity=FindingSeverity.HIGH,
                        message="The document loads remote content when opened.",
                        evidence=relation.get("Target", "")[:300],
                    )
                )
    return findings

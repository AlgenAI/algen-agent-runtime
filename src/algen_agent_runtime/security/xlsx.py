"""Inspect Excel (XLSX) workbooks before producing downstream-safe plain text.

Visible sheets are read in workbook order, one line per row with cells joined by `` | ``; a
formula contributes the value Excel last calculated, never the formula itself. Hidden sheets,
rows and columns are kept out of the sanitized text and reported as findings, like hidden runs in
a Word document. Comments are never extracted.
"""

from __future__ import annotations

import posixpath
import zipfile
from xml.etree import ElementTree

from algen_agent_runtime.security.documents import (
    DocumentSecurityFinding,
    DocumentSpan,
    ExtractedDocument,
    FindingSeverity,
    classify_findings,
    normalize_document_text,
    prompt_injection_findings,
    sanitize_untrusted_text,
)
from algen_agent_runtime.security.ooxml import (
    OFFICE_REL,
    PackageLimits,
    open_package,
    package_findings,
    parse_part,
    relationships,
)

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_KIND = "XLSX"
_TRUE = {"1", "true", "on"}


def _column_index(reference: str) -> int:
    """Zero-based column of a cell reference such as ``C12``."""
    index = 0
    for char in reference:
        if not char.isalpha():
            break
        index = index * 26 + (ord(char.upper()) - 64)
    return index - 1


def _text_of(element: ElementTree.Element) -> str:
    return "".join(node.text or "" for node in element.iter(f"{S}t"))


class XlsxDocumentExtractor:
    """Bounded, dependency-free XLSX inspection with hidden-content findings."""

    def __init__(
        self,
        *,
        limits: PackageLimits | None = None,
        max_characters: int = 250_000,
        max_rows_per_sheet: int = 5_000,
    ) -> None:
        self._limits = limits or PackageLimits()
        self._max_characters = max_characters
        self._max_rows = max_rows_per_sheet

    def extract(self, data: bytes, *, filename: str) -> ExtractedDocument:
        del filename
        with open_package(data, _KIND, self._limits) as archive:
            if "xl/workbook.xml" not in archive.namelist():
                raise ValueError("XLSX package has no xl/workbook.xml")
            findings = package_findings(archive, "xl/", _KIND)
            shared = self._shared_strings(archive)
            spans: list[DocumentSpan] = []
            lines: list[str] = []
            sheets = self._sheets(archive)
            for number, (name, path, hidden) in enumerate(sheets, start=1):
                if path not in archive.namelist():
                    continue
                rows, sheet_findings = self._rows(archive, path, shared, number)
                findings.extend(sheet_findings)
                text = "\n".join(rows)
                if not text:
                    continue
                if hidden:
                    findings.append(
                        DocumentSecurityFinding(
                            kind="hidden_text.hidden_sheet",
                            severity=FindingSeverity.HIGH,
                            message=f"Sheet {name!r} is hidden.",
                            page=number,
                            evidence=text[:300],
                        )
                    )
                    spans.append(DocumentSpan(text=text, page=number, suspicious=True))
                    continue
                injection = prompt_injection_findings(text, page=number)
                findings.extend(injection)
                spans.append(DocumentSpan(text=text, page=number, suspicious=bool(injection)))
                lines.append(f"--- Sheet: {name} ---\n{text}")
        if sum(len(span.text) for span in spans) > self._max_characters:
            raise ValueError(f"XLSX exceeds {self._max_characters} extracted character limit")
        sanitized, visible_findings = sanitize_untrusted_text("\n\n".join(lines))
        findings.extend(visible_findings)
        unique_findings, disposition = classify_findings(findings)
        return ExtractedDocument(
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            text="\n".join(span.text for span in spans),
            sanitized_text=sanitized,
            spans=tuple(spans),
            findings=unique_findings,
            disposition=disposition,
            page_count=max(1, len(sheets)),
        )

    @staticmethod
    def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
        if "xl/sharedStrings.xml" not in archive.namelist():
            return []
        root = parse_part(archive.read("xl/sharedStrings.xml"), _KIND)
        return [_text_of(item) for item in root.findall(f"{S}si")]

    @staticmethod
    def _sheets(archive: zipfile.ZipFile) -> list[tuple[str, str, bool]]:
        """(name, part path, hidden) for each sheet, in workbook order."""
        workbook = parse_part(archive.read("xl/workbook.xml"), _KIND)
        targets = relationships(archive, "xl/_rels/workbook.xml.rels", _KIND)
        sheets = []
        for sheet in workbook.iter(f"{S}sheet"):
            target = targets.get(sheet.get(f"{OFFICE_REL}id", ""), "")
            if not target:
                continue
            path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            hidden = sheet.get("state", "visible") in {"hidden", "veryHidden"}
            sheets.append((sheet.get("name", ""), posixpath.normpath(path), hidden))
        return sheets

    def _rows(
        self, archive: zipfile.ZipFile, path: str, shared: list[str], number: int
    ) -> tuple[list[str], list[DocumentSecurityFinding]]:
        root = parse_part(archive.read(path), _KIND)
        hidden_columns: set[int] = set()
        for column in root.iter(f"{S}col"):
            if column.get("hidden", "").lower() in _TRUE:
                first, last = int(column.get("min", "1")), int(column.get("max", "1"))
                # 1-based and inclusive; Excel has at most 16,384 columns.
                hidden_columns.update(range(first - 1, min(last, 16_384)))
        rows: list[str] = []
        hidden_text: list[str] = []
        for row in root.iter(f"{S}row"):
            if len(rows) >= self._max_rows:
                break
            row_hidden = row.get("hidden", "").lower() in _TRUE
            cells: list[str] = []
            for cell in row.findall(f"{S}c"):
                value = self._cell_value(cell, shared)
                if not value:
                    continue
                if row_hidden or _column_index(cell.get("r", "A")) in hidden_columns:
                    hidden_text.append(value)
                else:
                    cells.append(value)
            if cells:
                rows.append(" | ".join(cells))
        findings = []
        if hidden_text:
            findings.append(
                DocumentSecurityFinding(
                    kind="hidden_text.hidden_cells",
                    severity=FindingSeverity.HIGH,
                    message="Rows or columns holding text are hidden.",
                    page=number,
                    evidence=" ".join(hidden_text)[:300],
                )
            )
        return rows, findings

    @staticmethod
    def _cell_value(cell: ElementTree.Element, shared: list[str]) -> str:
        kind = cell.get("t", "n")
        if kind == "inlineStr":
            inline = cell.find(f"{S}is")
            return normalize_document_text(_text_of(inline)) if inline is not None else ""
        value = cell.find(f"{S}v")
        raw = (value.text or "") if value is not None else ""
        if kind == "s":
            index = int(raw) if raw.isdigit() else -1
            raw = shared[index] if 0 <= index < len(shared) else ""
        return normalize_document_text(raw)

"""Inspect Excel (XLSX) workbooks before producing downstream-safe plain text.

Visible sheets are read in workbook order, one line per row with cells joined by `` | ``; a
formula contributes the value Excel last calculated, never the formula itself. Numbers formatted
as dates or times come out in ISO form (``2023-11-01``, ``2023-11-01T09:30:00``, ``09:30:00``).
Hidden sheets, rows and columns are kept out of the sanitized text and reported as findings, like
hidden runs in a Word document. A sheet longer than ``max_rows_per_sheet`` is cut there and
reported as ``document.truncated``. Comments are never extracted.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from datetime import datetime, timedelta
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
# Built-in number formats: 14-17 dates, 18-21 times, 22 date and time (ECMA-376, 18.8.30).
_DATE_FORMATS = frozenset(range(14, 18)) | {22}
_TIME_FORMATS = frozenset(range(18, 22))
# Quoted text, escaped or padding characters and [colour]/[$-locale]/[h] sections hold no codes.
_FORMAT_NOISE = re.compile(r'"[^"]*"|\\.|_.|\*.|\[[^\]]*\]')
_ELAPSED = re.compile(r"\[(?:h+|m+|s+)\]", re.IGNORECASE)
_EPOCHS = {False: datetime(1899, 12, 30), True: datetime(1904, 1, 1)}


def _format_kind(code: str) -> str:
    """``date``, ``time`` or ``""`` for a custom number format code such as ``dd/mm/yyyy``."""
    section = code.split(";")[0]
    if _ELAPSED.search(section):
        return ""  # an elapsed duration such as [h]:mm, not a clock time
    tokens = _FORMAT_NOISE.sub("", section).lower()
    if "d" in tokens or "y" in tokens:
        return "date"
    if "h" in tokens or "s" in tokens:
        return "time"
    return "date" if "m" in tokens else ""


def _as_date(serial: str, kind: str, date1904: bool) -> str:
    """ISO text for a date or time serial; the serial itself when it is not a usable number."""
    try:
        value = float(serial)
        if value < 0:
            return serial
        moment = _EPOCHS[date1904] + timedelta(days=value)
    except (ValueError, OverflowError):
        return serial
    moment = moment.replace(microsecond=0) + timedelta(seconds=round(moment.microsecond / 1e6))
    if kind == "time":
        return moment.strftime("%H:%M:%S")
    if value == int(value):
        return moment.date().isoformat()
    return moment.isoformat()


# Shared strings, date or time kind by cell style index, and whether dates count from 1904.
_Workbook = tuple[list[str], dict[int, str], bool]


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
            workbook = (
                self._shared_strings(archive),
                self._date_styles(archive),
                self._date1904(archive),
            )
            spans: list[DocumentSpan] = []
            lines: list[str] = []
            sheets = self._sheets(archive)
            for number, (name, path, hidden) in enumerate(sheets, start=1):
                if path not in archive.namelist():
                    continue
                rows, sheet_findings = self._rows(archive, path, workbook, number)
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
    def _date_styles(archive: zipfile.ZipFile) -> dict[int, str]:
        """Cell style index to ``date`` or ``time`` for styles whose number format shows one."""
        if "xl/styles.xml" not in archive.namelist():
            return {}
        root = parse_part(archive.read("xl/styles.xml"), _KIND)
        custom = {
            int(item.get("numFmtId", "")): _format_kind(item.get("formatCode", ""))
            for item in root.iter(f"{S}numFmt")
            if item.get("numFmtId", "").isdigit()
        }
        cell_formats = root.find(f"{S}cellXfs")
        kinds: dict[int, str] = {}
        for index, xf in enumerate(cell_formats if cell_formats is not None else ()):
            number = xf.get("numFmtId", "0")
            fmt = int(number) if number.isdigit() else 0
            kind = custom.get(fmt) or (
                "date" if fmt in _DATE_FORMATS else "time" if fmt in _TIME_FORMATS else ""
            )
            if kind:
                kinds[index] = kind
        return kinds

    @staticmethod
    def _date1904(archive: zipfile.ZipFile) -> bool:
        workbook = parse_part(archive.read("xl/workbook.xml"), _KIND)
        properties = workbook.find(f"{S}workbookPr")
        return properties is not None and properties.get("date1904", "").lower() in _TRUE

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
        self, archive: zipfile.ZipFile, path: str, workbook: _Workbook, number: int
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
        truncated = False
        for row in root.iter(f"{S}row"):
            row_hidden = row.get("hidden", "").lower() in _TRUE
            cells: list[str] = []
            position = -1
            for cell in row.findall(f"{S}c"):
                # ``r`` is optional: a cell without one follows the previous cell in its row.
                reference = cell.get("r")
                position = _column_index(reference) if reference else position + 1
                value = self._cell_value(cell, workbook)
                if not value:
                    continue
                if row_hidden or position in hidden_columns:
                    hidden_text.append(value)
                else:
                    cells.append(value)
            if cells:
                if len(rows) >= self._max_rows:
                    truncated = True
                    break
                rows.append(" | ".join(cells))
        findings = []
        if truncated:
            findings.append(
                DocumentSecurityFinding(
                    kind="document.truncated",
                    severity=FindingSeverity.MEDIUM,
                    message=f"Only the first {self._max_rows} rows of the sheet were read.",
                    page=number,
                )
            )
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
    def _cell_value(cell: ElementTree.Element, workbook: _Workbook) -> str:
        shared, styles, date1904 = workbook
        kind = cell.get("t", "n")
        if kind == "inlineStr":
            inline = cell.find(f"{S}is")
            return normalize_document_text(_text_of(inline)) if inline is not None else ""
        value = cell.find(f"{S}v")
        raw = (value.text or "") if value is not None else ""
        if kind == "s":
            index = int(raw) if raw.isdigit() else -1
            raw = shared[index] if 0 <= index < len(shared) else ""
        elif kind == "n" and raw:
            style = cell.get("s", "0")
            date_kind = styles.get(int(style)) if style.isdigit() else None
            if date_kind:
                raw = _as_date(raw, date_kind, date1904)
        return normalize_document_text(raw)


from algen_agent_runtime.security._moved import warn_moved  # noqa: E402

warn_moved(__name__)

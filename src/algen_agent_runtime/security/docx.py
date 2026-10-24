"""Inspect Word (DOCX) documents before producing downstream-safe plain text.

Only the parts a reader sees are read: the body, headers, footers, footnotes and endnotes.
Comments, tracked deletions and field codes are never extracted. Runs a reader cannot see
(hidden, white or tiny text) are kept out of the sanitized text and reported as findings.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterator
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
from algen_agent_runtime.security.docx_contrast import (
    W,
    hex_color,
    is_invisible,
    page_background,
    paragraphs_with_background,
    run_background,
)

_W = W
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_VISIBLE_PARTS = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")
# Relationship types that pull remote content when the document opens; hyperlinks are normal.
_ACTIVE_EXTERNAL = re.compile(r"/(attachedTemplate|oleObject|frame|subDocument)$")
_FALSE = {"0", "false", "off"}


def _flag_on(properties: ElementTree.Element | None, name: str) -> bool:
    element = properties.find(f"{_W}{name}") if properties is not None else None
    return element is not None and element.get(f"{_W}val", "true").lower() not in _FALSE


def _half_points(properties: ElementTree.Element | None) -> float | None:
    size = properties.find(f"{_W}sz") if properties is not None else None
    raw = size.get(f"{_W}val") if size is not None else None
    return int(raw) / 2 if raw and raw.isdigit() else None


def _own_runs(paragraph: ElementTree.Element) -> Iterator[ElementTree.Element]:
    """Runs of this paragraph, including those in hyperlinks and inserted revisions.

    Text boxes nest whole paragraphs inside a run's drawing; those are visited as
    paragraphs of their own, so the walk stops at runs and at nested paragraphs.
    """
    pending = list(reversed(paragraph))
    while pending:
        element = pending.pop()
        if element.tag == f"{_W}r":
            yield element
        elif element.tag not in {f"{_W}p", f"{_W}del"}:
            pending.extend(reversed(element))


def _run_text(run: ElementTree.Element) -> str:
    parts: list[str] = []
    for child in run:
        if child.tag == f"{_W}t":
            parts.append(child.text or "")
        elif child.tag == f"{_W}tab":
            parts.append("\t")
        elif child.tag in {f"{_W}br", f"{_W}cr"}:
            parts.append("\n")
        elif child.tag == f"{_W}noBreakHyphen":
            parts.append("-")
    return "".join(parts)


class DocxDocumentExtractor:
    """Bounded, dependency-free DOCX inspection mirroring the PDF extractor's findings."""

    def __init__(
        self,
        *,
        max_bytes: int = 10_000_000,
        max_members: int = 1_000,
        max_uncompressed_bytes: int = 50_000_000,
        max_compression_ratio: int = 200,
        max_characters: int = 250_000,
        tiny_font_points: float = 2.0,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_members = max_members
        self._max_uncompressed_bytes = max_uncompressed_bytes
        self._max_compression_ratio = max_compression_ratio
        self._max_characters = max_characters
        self._tiny_font_points = tiny_font_points

    def extract(self, data: bytes, *, filename: str) -> ExtractedDocument:
        del filename
        if len(data) > self._max_bytes:
            raise ValueError(f"document exceeds {self._max_bytes} byte limit")
        if not data.startswith(b"PK\x03\x04"):
            raise ValueError("file content is not a DOCX package")
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ValueError("file content is not a DOCX package") from exc
        with archive:
            members = self._checked_members(archive)
            if "word/document.xml" not in members:
                raise ValueError("DOCX package has no word/document.xml")
            findings = self._package_findings(archive, members)
            spans: list[DocumentSpan] = []
            visible_paragraphs: list[str] = []
            for name in sorted(members, key=lambda item: (item != "word/document.xml", item)):
                if _VISIBLE_PARTS.match(name):
                    root = self._parse(archive.read(name))
                    for visible, part_spans, part_findings in self._paragraphs(root):
                        spans.extend(part_spans)
                        findings.extend(part_findings)
                        if visible:
                            visible_paragraphs.append(visible)
        if sum(len(span.text) for span in spans) > self._max_characters:
            raise ValueError(f"DOCX exceeds {self._max_characters} extracted character limit")
        sanitized, visible_findings = sanitize_untrusted_text("\n".join(visible_paragraphs))
        findings.extend(visible_findings)
        image_count = sum(1 for name in members if name.startswith("word/media/"))
        if not spans and image_count:
            findings.append(
                DocumentSecurityFinding(
                    kind="document.ocr_required",
                    severity=FindingSeverity.MEDIUM,
                    message="The document contains images but no text; OCR review is required.",
                )
            )
        unique_findings, disposition = classify_findings(findings)
        return ExtractedDocument(
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            text="\n".join(span.text for span in spans),
            sanitized_text=sanitized,
            spans=tuple(spans),
            findings=unique_findings,
            disposition=disposition,
            image_count=image_count,
        )

    def _checked_members(self, archive: zipfile.ZipFile) -> set[str]:
        infos = archive.infolist()
        if len(infos) > self._max_members:
            raise ValueError(f"DOCX package has more than {self._max_members} parts")
        total = 0
        for info in infos:
            total += info.file_size
            ratio = info.file_size / max(1, info.compress_size)
            if total > self._max_uncompressed_bytes or ratio > self._max_compression_ratio:
                raise ValueError("DOCX package expands beyond the configured limits")
        return {info.filename for info in infos}

    @staticmethod
    def _parse(payload: bytes) -> ElementTree.Element:
        # DOCX parts never declare a DTD; refusing one rules out entity-expansion attacks.
        if b"<!DOCTYPE" in payload[:4096].upper() or b"<!ENTITY" in payload.upper():
            raise ValueError("DOCX part declares a DTD, which is not allowed")
        try:
            return ElementTree.fromstring(payload)
        except ElementTree.ParseError as exc:
            raise ValueError("DOCX part is not well-formed XML") from exc

    def _package_findings(
        self, archive: zipfile.ZipFile, members: set[str]
    ) -> list[DocumentSecurityFinding]:
        findings: list[DocumentSecurityFinding] = []
        if any(name.lower().endswith("vbaproject.bin") for name in members):
            findings.append(
                DocumentSecurityFinding(
                    kind="document.macros",
                    severity=FindingSeverity.CRITICAL,
                    message="The document carries a macro project.",
                )
            )
        for name in members:
            if not (name.startswith("word/_rels/") and name.endswith(".rels")):
                continue
            for relation in self._parse(archive.read(name)).iter(f"{_REL}Relationship"):
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

    def _paragraphs(
        self, root: ElementTree.Element
    ) -> Iterator[tuple[str, list[DocumentSpan], list[DocumentSecurityFinding]]]:
        for paragraph, background in paragraphs_with_background(root, page_background(root)):
            spans: list[DocumentSpan] = []
            findings: list[DocumentSecurityFinding] = []
            visible: list[str] = []
            for run in _own_runs(paragraph):
                text = normalize_document_text(_run_text(run))
                if not text:
                    continue
                span, run_findings = self._inspect_run(run, text, background)
                spans.append(span)
                findings.extend(run_findings)
                if not span.suspicious:
                    visible.append(text)
            line = normalize_document_text(" ".join(visible))
            injection = prompt_injection_findings(line, page=1) if line else ()
            if injection:
                findings.extend(injection)
                spans = [span.model_copy(update={"suspicious": True}) for span in spans]
                line = ""
            yield line, spans, findings

    def _inspect_run(
        self, run: ElementTree.Element, text: str, background: int
    ) -> tuple[DocumentSpan, list[DocumentSecurityFinding]]:
        properties = run.find(f"{_W}rPr")
        size = _half_points(properties)
        color_element = properties.find(f"{_W}color") if properties is not None else None
        color = hex_color(color_element.get(f"{_W}val")) if color_element is not None else None
        hidden = _flag_on(properties, "vanish") or _flag_on(properties, "webHidden")
        tiny = size is not None and size < self._tiny_font_points
        pale = is_invisible(color, run_background(properties, background))
        findings = [
            DocumentSecurityFinding(
                kind=kind, severity=FindingSeverity.HIGH, message=message, evidence=text[:300]
            )
            for flagged, kind, message in (
                (hidden, "hidden_text.hidden_run", "Text is formatted as hidden."),
                (tiny, "hidden_text.tiny_font", "Text uses an unusually small font."),
                (pale, "hidden_text.low_visibility", "Text nearly matches its background."),
            )
            if flagged
        ]
        span = DocumentSpan(
            text=text, page=1, font_size=size, color=color, suspicious=bool(findings)
        )
        return span, findings

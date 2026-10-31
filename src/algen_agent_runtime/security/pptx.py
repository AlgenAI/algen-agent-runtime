"""Inspect PowerPoint (PPTX) presentations before producing downstream-safe plain text.

Slides are read in presentation order, one line per paragraph of each shape and table cell.
Hidden slides and hidden shapes are kept out of the sanitized text and reported as findings.
Speaker notes and comments, which an audience never sees, are not extracted.
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

P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_KIND = "PPTX"
_SHAPES = {f"{P}sp", f"{P}graphicFrame", f"{P}pic", f"{P}cxnSp"}
_FALSE = {"0", "false", "off"}


def _hidden(shape: ElementTree.Element) -> bool:
    properties = next(shape.iter(f"{P}cNvPr"), None)
    return properties is not None and properties.get("hidden", "0").lower() not in _FALSE


def _paragraphs(shape: ElementTree.Element) -> list[str]:
    lines = []
    for paragraph in shape.iter(f"{A}p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == f"{A}t":
                parts.append(node.text or "")
            elif node.tag == f"{A}br":
                parts.append("\n")
        line = normalize_document_text("".join(parts))
        if line:
            lines.append(line)
    return lines


class PptxDocumentExtractor:
    """Bounded, dependency-free PPTX inspection with hidden-content findings."""

    def __init__(self, *, limits: PackageLimits | None = None, max_characters: int = 250_000):
        self._limits = limits or PackageLimits()
        self._max_characters = max_characters

    def extract(self, data: bytes, *, filename: str) -> ExtractedDocument:
        del filename
        with open_package(data, _KIND, self._limits) as archive:
            if "ppt/presentation.xml" not in archive.namelist():
                raise ValueError("PPTX package has no ppt/presentation.xml")
            findings = package_findings(archive, "ppt/", _KIND)
            spans: list[DocumentSpan] = []
            blocks: list[str] = []
            slides = self._slides(archive)
            image_count = sum(1 for name in archive.namelist() if name.startswith("ppt/media/"))
            for number, path in enumerate(slides, start=1):
                root = parse_part(archive.read(path), _KIND)
                slide_hidden = root.get("show", "1").lower() in _FALSE
                visible, hidden = self._shape_text(root)
                if slide_hidden:
                    hidden, visible = hidden + visible, []
                if hidden:
                    text = "\n".join(hidden)
                    findings.append(
                        DocumentSecurityFinding(
                            kind="hidden_text.hidden_slide"
                            if slide_hidden
                            else "hidden_text.hidden_shape",
                            severity=FindingSeverity.HIGH,
                            message="A hidden slide holds text."
                            if slide_hidden
                            else "A hidden shape holds text.",
                            page=number,
                            evidence=text[:300],
                        )
                    )
                    spans.append(DocumentSpan(text=text, page=number, suspicious=True))
                if visible:
                    text = "\n".join(visible)
                    injection = prompt_injection_findings(text, page=number)
                    findings.extend(injection)
                    spans.append(DocumentSpan(text=text, page=number, suspicious=bool(injection)))
                    blocks.append(f"--- Slide {number} ---\n{text}")
        if sum(len(span.text) for span in spans) > self._max_characters:
            raise ValueError(f"PPTX exceeds {self._max_characters} extracted character limit")
        sanitized, visible_findings = sanitize_untrusted_text("\n\n".join(blocks))
        findings.extend(visible_findings)
        # Visible text flagged as injection is still text: OCR is for decks with none at all.
        if not blocks and image_count:
            findings.append(
                DocumentSecurityFinding(
                    kind="document.ocr_required",
                    severity=FindingSeverity.MEDIUM,
                    message="The presentation contains images but no text; OCR review is required.",
                )
            )
        elif image_count:
            findings.append(
                DocumentSecurityFinding(
                    kind="document.images_excluded",
                    severity=FindingSeverity.LOW,
                    message="Embedded images were excluded from downstream screening.",
                )
            )
        unique_findings, disposition = classify_findings(findings)
        return ExtractedDocument(
            media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            text="\n".join(span.text for span in spans),
            sanitized_text=sanitized,
            spans=tuple(spans),
            findings=unique_findings,
            disposition=disposition,
            page_count=max(1, len(slides)),
            image_count=image_count,
        )

    @staticmethod
    def _slides(archive: zipfile.ZipFile) -> list[str]:
        """Slide part paths in presentation order."""
        presentation = parse_part(archive.read("ppt/presentation.xml"), _KIND)
        targets = relationships(archive, "ppt/_rels/presentation.xml.rels", _KIND)
        names = set(archive.namelist())
        paths = []
        for slide in presentation.iter(f"{P}sldId"):
            target = targets.get(slide.get(f"{OFFICE_REL}id", ""), "")
            path = posixpath.normpath(
                target.lstrip("/") if target.startswith("/") else f"ppt/{target}"
            )
            if target and path in names:
                paths.append(path)
        return paths

    @staticmethod
    def _shape_text(root: ElementTree.Element) -> tuple[list[str], list[str]]:
        """(visible lines, hidden lines) of a slide; a hidden group hides everything in it."""
        visible: list[str] = []
        hidden: list[str] = []
        tree = next(root.iter(f"{P}spTree"), None)
        pending = [(child, False) for child in reversed(tree)] if tree is not None else []
        while pending:
            element, parent_hidden = pending.pop()
            if element.tag == f"{P}grpSp":
                group_hidden = parent_hidden or _hidden(element)
                pending.extend((child, group_hidden) for child in reversed(element))
            elif element.tag in _SHAPES:
                lines = _paragraphs(element)
                (hidden if parent_hidden or _hidden(element) else visible).extend(lines)
        return visible, hidden


from algen_agent_runtime.security._moved import warn_moved  # noqa: E402

warn_moved(__name__)

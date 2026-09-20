from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class DocumentDisposition(StrEnum):
    PASS = "pass"
    REVIEW = "review"
    QUARANTINE = "quarantine"


class FindingSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DocumentSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    page: int = Field(ge=1)
    font_size: float | None = None
    color: int | None = None
    opacity: float | None = None
    bbox: tuple[float, float, float, float] | None = None
    suspicious: bool = False


class DocumentSecurityFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    severity: FindingSeverity
    message: str
    page: int | None = None
    evidence: str | None = Field(default=None, max_length=300)


class ExtractedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    media_type: str
    text: str
    sanitized_text: str
    spans: tuple[DocumentSpan, ...]
    findings: tuple[DocumentSecurityFinding, ...] = ()
    disposition: DocumentDisposition = DocumentDisposition.PASS
    page_count: int = Field(default=1, ge=1)
    image_count: int = Field(default=0, ge=0)


class DocumentExtractor(Protocol):
    def extract(self, data: bytes, *, filename: str) -> ExtractedDocument: ...


_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(r"\b(ignore|disregard|forget)\b.{0,50}\b(previous|prior|above|system)\b", re.I),
    ),
    (
        "screening_bypass",
        re.compile(r"\b(bypass|override|skip)\b.{0,50}\b(screen|policy|guardrail|filter)\b", re.I),
    ),
    (
        "score_manipulation",
        re.compile(
            r"\b(rank|rate|score|select|hire)\b.{0,40}\b(100|maximum|highest|perfect|candidate)\b",
            re.I,
        ),
    ),
    (
        "prompt_exfiltration",
        re.compile(
            r"\b(reveal|print|repeat|expose)\b.{0,50}\b(system prompt|hidden instructions?|secrets?)\b",
            re.I,
        ),
    ),
    ("role_impersonation", re.compile(r"(?:^|\n)\s*(system|assistant|developer)\s*:", re.I)),
)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ().-]{7,}\d)(?!\w)")
_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_ADDRESS_LINE = re.compile(
    r"(?:\baddress\s*:|\b\d{1,6}\s+[A-Za-z0-9 .'-]+\s(?:street|st|road|rd|avenue|ave|lane|ln|boulevard|blvd|drive|dr)\b)",
    re.I,
)
_DEMOGRAPHIC_LINE = re.compile(
    r"\b(?:gender|sex|ethnicity|race|religion|marital status|nationality)\s*:", re.I
)


def normalize_document_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).replace("\x00", "")
    normalized = "".join(
        character for character in normalized if unicodedata.category(character) != "Cf"
    )
    return "\n".join(" ".join(line.split()) for line in normalized.splitlines()).strip()


def normalize_pdf_opacity(value: object) -> float:
    """Normalize PDF extractor alpha values expressed as either 0..1 or 0..255."""
    if value is None:
        return 1.0
    if not isinstance(value, str | int | float):
        return 1.0
    try:
        alpha = float(value)
    except (TypeError, ValueError):
        return 1.0
    if alpha > 1:
        alpha /= 255
    return max(0.0, min(1.0, alpha))


def prompt_injection_findings(
    text: str, *, page: int | None = None
) -> tuple[DocumentSecurityFinding, ...]:
    findings: list[DocumentSecurityFinding] = []
    for kind, pattern in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            findings.append(
                DocumentSecurityFinding(
                    kind=f"prompt_injection.{kind}",
                    severity=FindingSeverity.CRITICAL
                    if kind == "instruction_override"
                    else FindingSeverity.HIGH,
                    message="The document contains text resembling instructions to an AI system.",
                    page=page,
                    evidence=" ".join(match.group(0).split())[:300],
                )
            )
    return tuple(findings)


def sanitize_untrusted_text(text: str) -> tuple[str, tuple[DocumentSecurityFinding, ...]]:
    normalized = normalize_document_text(text)
    findings = prompt_injection_findings(normalized)
    safe_lines = [
        line
        for line in normalized.splitlines()
        if not any(pattern.search(line) for _, pattern in _INJECTION_PATTERNS)
    ]
    return "\n".join(safe_lines).strip(), findings


def redact_pii(text: str, *, entities: tuple[str, ...] = ()) -> str:
    value = _EMAIL.sub("[EMAIL REDACTED]", text)
    value = _PHONE.sub("[PHONE REDACTED]", value)
    value = _URL.sub("[URL REDACTED]", value)
    for entity in sorted(
        (item.strip() for item in entities if item.strip()), key=len, reverse=True
    ):
        value = re.sub(re.escape(entity), "[IDENTITY REDACTED]", value, flags=re.I)
    lines = []
    for line in value.splitlines():
        if _ADDRESS_LINE.search(line):
            lines.append("[ADDRESS REDACTED]")
        elif _DEMOGRAPHIC_LINE.search(line):
            lines.append("[DEMOGRAPHIC DATA REDACTED]")
        else:
            lines.append(line)
    return "\n".join(lines)


class PlainTextDocumentExtractor:
    def __init__(self, *, max_bytes: int = 2_000_000, max_characters: int = 250_000) -> None:
        self._max_bytes = max_bytes
        self._max_characters = max_characters

    def extract(self, data: bytes, *, filename: str) -> ExtractedDocument:
        del filename
        if len(data) > self._max_bytes:
            raise ValueError(f"document exceeds {self._max_bytes} byte limit")
        text = data.decode("utf-8", errors="replace")
        if len(text) > self._max_characters:
            raise ValueError(f"document exceeds {self._max_characters} character limit")
        sanitized, findings = sanitize_untrusted_text(text)
        disposition = DocumentDisposition.QUARANTINE if findings else DocumentDisposition.PASS
        return ExtractedDocument(
            media_type="text/plain",
            text=normalize_document_text(text),
            sanitized_text=sanitized,
            spans=(DocumentSpan(text=text, page=1, suspicious=bool(findings)),),
            findings=findings,
            disposition=disposition,
        )


class PdfDocumentExtractor:
    """Inspect PDF text spans before producing downstream-safe plain text."""

    def __init__(
        self,
        *,
        max_bytes: int = 10_000_000,
        max_pages: int = 50,
        max_characters: int = 250_000,
        tiny_font_points: float = 2.0,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_pages = max_pages
        self._max_characters = max_characters
        self._tiny_font_points = tiny_font_points

    def extract(self, data: bytes, *, filename: str) -> ExtractedDocument:
        del filename
        if len(data) > self._max_bytes:
            raise ValueError(f"document exceeds {self._max_bytes} byte limit")
        if not data.startswith(b"%PDF-"):
            raise ValueError("file content is not a PDF")
        try:
            import fitz
        except ImportError as exc:
            raise ImportError("install algen-agent-runtime[hiring] for PDF inspection") from exc
        document = fitz.open(stream=data, filetype="pdf")
        if document.page_count < 1 or document.page_count > self._max_pages:
            raise ValueError(f"PDF page count must be between 1 and {self._max_pages}")
        spans: list[DocumentSpan] = []
        findings: list[DocumentSecurityFinding] = []
        image_count = 0
        for page_index, page in enumerate(document, start=1):
            image_count += len(page.get_images(full=True))
            width, height = float(page.rect.width), float(page.rect.height)
            payload = page.get_text("dict")
            for block in payload.get("blocks", ()):
                for line in block.get("lines", ()):
                    for raw in line.get("spans", ()):
                        text = normalize_document_text(str(raw.get("text", "")))
                        if not text:
                            continue
                        raw_bbox = tuple(float(item) for item in raw.get("bbox", (0, 0, 0, 0)))
                        bbox: tuple[float, float, float, float] = (
                            (raw_bbox[0], raw_bbox[1], raw_bbox[2], raw_bbox[3])
                            if len(raw_bbox) >= 4
                            else (0.0, 0.0, 0.0, 0.0)
                        )
                        size = float(raw.get("size", 0))
                        color = int(raw.get("color", 0))
                        opacity = normalize_pdf_opacity(raw.get("alpha"))
                        tiny = size < self._tiny_font_points
                        pale = all(
                            channel >= 245
                            for channel in ((color >> 16) & 255, (color >> 8) & 255, color & 255)
                        )
                        invisible = opacity <= 0.05
                        off_page = (
                            bbox[2] <= 0 or bbox[3] <= 0 or bbox[0] >= width or bbox[1] >= height
                        )
                        injection = prompt_injection_findings(text, page=page_index)
                        suspicious = tiny or pale or invisible or off_page or bool(injection)
                        spans.append(
                            DocumentSpan(
                                text=text,
                                page=page_index,
                                font_size=size,
                                color=color,
                                opacity=opacity,
                                bbox=bbox,
                                suspicious=suspicious,
                            )
                        )
                        if tiny:
                            findings.append(
                                DocumentSecurityFinding(
                                    kind="hidden_text.tiny_font",
                                    severity=FindingSeverity.HIGH,
                                    message="Text uses an unusually small font.",
                                    page=page_index,
                                    evidence=text[:300],
                                )
                            )
                        if pale or invisible:
                            findings.append(
                                DocumentSecurityFinding(
                                    kind="hidden_text.low_visibility",
                                    severity=FindingSeverity.HIGH,
                                    message="Text has very low visual contrast or opacity.",
                                    page=page_index,
                                    evidence=text[:300],
                                )
                            )
                        if off_page:
                            findings.append(
                                DocumentSecurityFinding(
                                    kind="hidden_text.off_page",
                                    severity=FindingSeverity.HIGH,
                                    message="Text is positioned outside the visible page.",
                                    page=page_index,
                                    evidence=text[:300],
                                )
                            )
                        findings.extend(injection)
        if sum(len(span.text) for span in spans) > self._max_characters:
            raise ValueError(f"PDF exceeds {self._max_characters} extracted character limit")
        visible = "\n".join(span.text for span in spans if not span.suspicious)
        sanitized, visible_findings = sanitize_untrusted_text(visible)
        findings.extend(visible_findings)
        if not spans and image_count:
            findings.append(
                DocumentSecurityFinding(
                    kind="document.ocr_required",
                    severity=FindingSeverity.MEDIUM,
                    message="The PDF contains images but no extractable text; OCR review is required.",
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
        deduplicated: list[DocumentSecurityFinding] = []
        finding_keys: set[tuple[str, int | None]] = set()
        for finding in findings:
            key = (finding.kind, finding.page)
            if key not in finding_keys:
                finding_keys.add(key)
                deduplicated.append(finding)
        findings = deduplicated
        highest = {finding.severity for finding in findings}
        disposition = (
            DocumentDisposition.QUARANTINE
            if FindingSeverity.CRITICAL in highest or FindingSeverity.HIGH in highest
            else DocumentDisposition.REVIEW
            if findings
            else DocumentDisposition.PASS
        )
        return ExtractedDocument(
            media_type="application/pdf",
            text="\n".join(span.text for span in spans),
            sanitized_text=sanitized,
            spans=tuple(spans),
            findings=tuple(findings),
            disposition=disposition,
            page_count=document.page_count,
            image_count=image_count,
        )


def extractor_for(filename: str) -> DocumentExtractor:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return PdfDocumentExtractor()
    if suffix in {".txt", ".md"}:
        return PlainTextDocumentExtractor()
    raise ValueError("only PDF, TXT, and Markdown documents are supported")

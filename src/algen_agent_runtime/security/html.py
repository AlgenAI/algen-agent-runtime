"""Inspect HTML documents before producing downstream-safe plain text.

Only the text a reader sees in the body is extracted: scripts, styles, templates, the head and
comments never are. Text an inline style or the ``hidden`` attribute hides (``display: none``,
``visibility: hidden``, a zero font size or opacity) is kept out of the sanitized text and
reported as a finding. Styles from stylesheets are not evaluated.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

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

_SKIPPED = {"script", "style", "noscript", "template", "head", "svg", "math", "iframe", "object"}
_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
    "track", "wbr",
}  # fmt: skip
_BLOCK = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
    "li", "main", "nav", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
}  # fmt: skip
_HIDING_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:\.0+)?(?:px|pt|em|rem|%)?\s*(?:;|$)"
    r"|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)",
    re.I,
)


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, bool]] = []  # (tag, skipped, hidden)
        self.visible: list[str] = []
        self.hidden: list[str] = []

    def _state(self) -> tuple[bool, bool]:
        return (
            any(skipped for _, skipped, _ in self.stack),
            any(hidden for _, _, hidden in self.stack),
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK:
            self.visible.append("\n")
        if tag in _VOID:
            return
        values = {name.lower(): value or "" for name, value in attrs}
        hidden = "hidden" in values or bool(_HIDING_STYLE.search(values.get("style", "")))
        self.stack.append((tag, tag in _SKIPPED, hidden))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in _BLOCK:
            self.visible.append("\n")

    def handle_data(self, data: str) -> None:
        skipped, hidden = self._state()
        if skipped or not data.strip():
            return
        (self.hidden if hidden else self.visible).append(data)


class HtmlDocumentExtractor:
    """Bounded, dependency-free HTML inspection with hidden-content findings."""

    def __init__(self, *, max_bytes: int = 2_000_000, max_characters: int = 250_000) -> None:
        self._max_bytes = max_bytes
        self._max_characters = max_characters

    def extract(self, data: bytes, *, filename: str) -> ExtractedDocument:
        del filename
        if len(data) > self._max_bytes:
            raise ValueError(f"document exceeds {self._max_bytes} byte limit")
        collector = _Collector()
        collector.feed(data.decode("utf-8", errors="replace"))
        collector.close()
        lines = [
            line
            for line in (
                normalize_document_text(part) for part in "".join(collector.visible).split("\n")
            )
            if line
        ]
        hidden = normalize_document_text(" ".join(collector.hidden))
        if sum(len(line) for line in lines) + len(hidden) > self._max_characters:
            raise ValueError(f"HTML exceeds {self._max_characters} extracted character limit")
        findings: list[DocumentSecurityFinding] = []
        spans = [DocumentSpan(text=line, page=1) for line in lines]
        if hidden:
            findings.append(
                DocumentSecurityFinding(
                    kind="hidden_text.hidden_element",
                    severity=FindingSeverity.HIGH,
                    message="Text is hidden by an inline style or the hidden attribute.",
                    evidence=hidden[:300],
                )
            )
            spans.append(DocumentSpan(text=hidden, page=1, suspicious=True))
        text = "\n".join(lines)
        injection = prompt_injection_findings(text, page=1)
        findings.extend(injection)
        sanitized, visible_findings = sanitize_untrusted_text(text)
        findings.extend(visible_findings)
        unique_findings, disposition = classify_findings(findings)
        return ExtractedDocument(
            media_type="text/html",
            text="\n".join(span.text for span in spans),
            sanitized_text=sanitized,
            spans=tuple(spans),
            findings=unique_findings,
            disposition=disposition,
        )

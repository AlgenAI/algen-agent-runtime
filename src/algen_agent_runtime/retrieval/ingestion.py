from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from algen_agent_runtime.retrieval.contracts import RetrievedDocument, SourceDocument


@runtime_checkable
class Chunker(Protocol):
    """Splits one source document into the chunks a vector store indexes."""

    def chunk(self, document: SourceDocument) -> tuple[RetrievedDocument, ...]: ...


def _check_sizes(chunk_size: int, chunk_overlap: int) -> None:
    if chunk_size < 64:
        raise ValueError("chunk_size must be at least 64 characters")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be non-negative and smaller than chunk_size")


def _split_words(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """``text`` cut at spaces into pieces of at most ``chunk_size`` characters, with overlap."""
    pieces: list[str] = []
    start = 0
    while start < len(text):
        hard_end = min(len(text), start + chunk_size)
        end = hard_end
        if hard_end < len(text):
            boundary = text.rfind(" ", start + chunk_size // 2, hard_end)
            if boundary > start:
                end = boundary
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        start = max(start + 1, end - chunk_overlap)
    return pieces


@dataclass(frozen=True)
class TextChunker:
    """Deterministic, dependency-free text chunker with bounded overlap."""

    chunk_size: int = 1_200
    chunk_overlap: int = 200

    def __post_init__(self) -> None:
        _check_sizes(self.chunk_size, self.chunk_overlap)

    def chunk(self, document: SourceDocument) -> tuple[RetrievedDocument, ...]:
        text = " ".join(document.text.split())
        return tuple(
            RetrievedDocument(
                id=f"{document.id}:{index}",
                document_id=document.id,
                text=chunk_text,
                source=document.source,
                title=document.title,
                uri=document.uri,
                chunk_index=index,
                score=0,
                metadata=document.metadata,
            )
            for index, chunk_text in enumerate(
                _split_words(text, self.chunk_size, self.chunk_overlap)
            )
        )


_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_SETEXT_UNDERLINE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_FENCE_OPEN = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_TABLE_DELIMITER = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_NOT_PLAIN_TEXT = re.compile(r"^\s*([-*+>|]|\d+[.)])(\s|$)")
_INLINE_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_INLINE_MARKUP = re.compile(r"[`*_~]")
_ANCHOR_DROP = re.compile(r"[^\w\- ]")


def heading_anchor(heading: str) -> str:
    """The GitHub-style anchor slug of a heading: ``"Getting Started!"`` -> ``getting-started``."""
    text = _INLINE_LINK.sub(r"\1", unicodedata.normalize("NFC", heading))
    text = _INLINE_MARKUP.sub("", text).strip().lower()
    return _ANCHOR_DROP.sub("", text).replace(" ", "-")


def _heading_text(raw: str) -> str:
    return " ".join(_INLINE_MARKUP.sub("", _INLINE_LINK.sub(r"\1", raw)).split())


def _fence_opening(line: str) -> re.Match[str] | None:
    """The match for a line that opens a fenced code block, else ``None``."""
    opened = _FENCE_OPEN.match(line)
    # A backtick fence's info string may not contain a backtick (CommonMark).
    if opened and not (opened.group(2)[0] == "`" and "`" in opened.group(3)):
        return opened
    return None


def _closes(line: str, fence: str) -> bool:
    stripped = line.strip()
    return (
        len(line) - len(line.lstrip(" ")) <= 3
        and len(stripped) >= len(fence)
        and set(stripped) == {fence[0]}
    )


@dataclass
class _Section:
    path: tuple[str, ...]
    level: int
    heading_line: str | None
    anchor: str | None
    lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MarkdownChunker:
    """Heading-aware Markdown chunker.

    The document is split at its headings (ATX ``#`` headings and setext underlines, never
    inside a fenced code block). The chunks of a section carry ``heading_path`` (the heading
    titles from the top down), ``heading``, ``heading_level`` and ``anchor`` (a GitHub-style
    slug, made unique within the document) in their metadata; text before the first heading
    has none of these. With ``link_anchors``, a chunk's ``uri`` is ``<document uri>#<anchor>``
    when the document has a URI without a fragment, so a citation links to the section.

    Within a section, blocks (paragraphs, lists, tables and fenced code) are packed whole into
    chunks of at most ``chunk_size`` characters. A larger block is split at line ends: fenced
    code is re-fenced in each piece and a table repeats its header row. Only a single line
    longer than a chunk is cut at spaces, with ``chunk_overlap``. With ``include_heading_path``,
    each chunk starts with the section's heading path (``Guide > Install``) so it can be read,
    and retrieved, on its own; without it, the section's first chunk keeps the heading line.
    Whitespace inside blocks is preserved.
    """

    chunk_size: int = 1_200
    chunk_overlap: int = 200
    include_heading_path: bool = True
    link_anchors: bool = True

    def __post_init__(self) -> None:
        _check_sizes(self.chunk_size, self.chunk_overlap)

    def chunk(self, document: SourceDocument) -> tuple[RetrievedDocument, ...]:
        chunks: list[RetrievedDocument] = []
        for section in self._sections(document.text):
            prefix = ""
            if self.include_heading_path and section.path:
                prefix = self._breadcrumb(section.path) + "\n\n"
            lines = list(section.lines)
            if section.heading_line is not None and not prefix:
                lines.insert(0, section.heading_line)
            for body in self._pack(self._blocks(lines), self.chunk_size - len(prefix)):
                chunks.append(self._chunk(document, section, prefix + body, len(chunks)))
        return tuple(chunks)

    def _breadcrumb(self, path: tuple[str, ...]) -> str:
        breadcrumb = " > ".join(path)
        limit = self.chunk_size // 4
        return breadcrumb if len(breadcrumb) <= limit else breadcrumb[: limit - 3] + "..."

    def _chunk(
        self, document: SourceDocument, section: _Section, text: str, index: int
    ) -> RetrievedDocument:
        metadata: dict[str, Any] = dict(document.metadata)
        uri = document.uri
        if section.path:
            metadata.update(
                heading_path=list(section.path),
                heading=section.path[-1],
                heading_level=section.level,
            )
        if section.anchor:
            metadata["anchor"] = section.anchor
            if self.link_anchors and uri and "#" not in uri:
                uri = f"{uri}#{section.anchor}"
        return RetrievedDocument(
            id=f"{document.id}:{index}",
            document_id=document.id,
            text=text,
            source=document.source,
            title=document.title,
            uri=uri,
            chunk_index=index,
            score=0,
            metadata=metadata,
        )

    @staticmethod
    def _sections(text: str) -> list[_Section]:
        sections = [_Section(path=(), level=0, heading_line=None, anchor=None)]
        stack: list[tuple[int, str]] = []
        anchors: dict[str, int] = {}
        fence: str | None = None

        def start(level: int, title: str, heading_line: str) -> None:
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            slug = heading_anchor(title)
            anchor: str | None = None
            if slug:
                seen = anchors.get(slug)
                anchors[slug] = 0 if seen is None else seen + 1
                anchor = slug if seen is None else f"{slug}-{seen + 1}"
            sections.append(_Section(tuple(name for _, name in stack), level, heading_line, anchor))

        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            current = sections[-1]
            if fence is not None:
                current.lines.append(line)
                if _closes(line, fence):
                    fence = None
                continue
            opened = _fence_opening(line)
            if opened:
                fence = opened.group(2)
                current.lines.append(line)
                continue
            atx = _ATX_HEADING.match(line)
            if atx:
                start(len(atx.group(1)), _heading_text(atx.group(2) or ""), line.strip())
                continue
            underline = _SETEXT_UNDERLINE.match(line)
            previous = current.lines[-1] if current.lines else ""
            if (
                underline
                and previous.strip()
                and not _NOT_PLAIN_TEXT.match(previous)
                and (len(current.lines) < 2 or not current.lines[-2].strip())
            ):
                current.lines.pop()
                level = 1 if underline.group(1).startswith("=") else 2
                start(level, _heading_text(previous), f"{'#' * level} {previous.strip()}")
                continue
            current.lines.append(line)
        return [section for section in sections if any(line.strip() for line in section.lines)]

    @staticmethod
    def _blocks(lines: list[str]) -> list[list[str]]:
        """Blank-line separated blocks; a fenced code block is always one block."""
        blocks: list[list[str]] = []
        current: list[str] = []
        fence: str | None = None
        for line in lines:
            if fence is not None:
                current.append(line)
                if _closes(line, fence):
                    fence = None
                    blocks.append(current)
                    current = []
                continue
            opened = _fence_opening(line)
            if opened:
                if current:
                    blocks.append(current)
                current = [line]
                fence = opened.group(2)
                continue
            if not line.strip():
                if current:
                    blocks.append(current)
                    current = []
                continue
            current.append(line)
        if current:
            blocks.append(current)
        return blocks

    def _pack(self, blocks: list[list[str]], budget: int) -> list[str]:
        pieces: list[str] = []
        current = ""
        for block in blocks:
            for part in self._fit(block, budget):
                candidate = f"{current}\n\n{part}" if current else part
                if len(candidate) <= budget:
                    current = candidate
                    continue
                if current:
                    pieces.append(current)
                current = part
        if current:
            pieces.append(current)
        return pieces

    def _fit(self, block: list[str], budget: int) -> list[str]:
        """``block`` as one text, or as pieces of at most ``budget`` characters."""
        text = "\n".join(block)
        if len(text) <= budget:
            return [text]
        opened = _fence_opening(block[0])
        if opened and len(block) > 1:
            fence = opened.group(2)
            closed = _closes(block[-1], fence)
            body = block[1:-1] if closed else block[1:]
            closing = block[-1] if closed else opened.group(1) + fence
            room = budget - len(block[0]) - len(closing) - 2
            if room >= budget // 4:
                return [f"{block[0]}\n{piece}\n{closing}" for piece in self._lines(body, room)]
        if len(block) > 2 and "|" in block[0] and _TABLE_DELIMITER.match(block[1]):
            header = f"{block[0]}\n{block[1]}"
            if len(header) < budget // 2:
                return [
                    f"{header}\n{piece}"
                    for piece in self._lines(block[2:], budget - len(header) - 1)
                ]
        return self._lines(block, budget)

    def _lines(self, lines: list[str], budget: int) -> list[str]:
        """``lines`` grouped into pieces of at most ``budget`` characters, cut at line ends
        unless one line alone is longer than that."""
        pieces: list[str] = []
        current: list[str] = []
        size = 0
        for line in lines:
            if len(line) > budget:
                if current:
                    pieces.append("\n".join(current))
                    current, size = [], 0
                overlap = min(self.chunk_overlap, budget // 2)
                pieces.extend(_split_words(line, budget, overlap))
                continue
            added = len(line) + (1 if current else 0)
            if current and size + added > budget:
                pieces.append("\n".join(current))
                current, size = [], 0
                added = len(line)
            current.append(line)
            size += added
        if current:
            pieces.append("\n".join(current))
        return pieces

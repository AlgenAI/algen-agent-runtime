from __future__ import annotations

import pytest

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.context.builder import RetrievalContextBuilder
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.retrieval.contracts import RetrievalMode, RetrievalQuery, SourceDocument
from algen_agent_runtime.retrieval.ingestion import (
    Chunker,
    MarkdownChunker,
    TextChunker,
    heading_anchor,
)
from algen_agent_runtime.retrieval.memory import InMemoryRetriever

GUIDE = """Intro paragraph before any heading.

# Guide

Welcome to the guide.

## Install

Run the installer.

```bash
pip install example

# not a heading: inside a fence
```

## Configure

| Key | Meaning |
| --- | --- |
| a | first |

## Install

A second section with the same title.

Setext Title
------------

Body under a setext heading.
"""


def markdown(text: str, *, uri: str | None = "https://docs.example/guide") -> SourceDocument:
    return SourceDocument(
        id="guide",
        text=text,
        source="docs://guide",
        title="Guide",
        uri=uri,
        metadata={"tenant_id": "tenant-a", "category": "docs"},
    )


def test_heading_anchor_matches_github_slugs() -> None:
    assert heading_anchor("Getting Started!") == "getting-started"
    assert heading_anchor("Use `pip` with [PyPI](https://pypi.org)") == "use-pip-with-pypi"
    assert heading_anchor("API v2.0 — notes") == "api-v20--notes"
    assert heading_anchor("Über uns") == "über-uns"


def test_markdown_chunker_splits_by_heading_and_records_path_and_anchor() -> None:
    chunks = MarkdownChunker().chunk(markdown(GUIDE))

    assert [chunk.metadata.get("heading_path") for chunk in chunks] == [
        None,
        ["Guide"],
        ["Guide", "Install"],
        ["Guide", "Configure"],
        ["Guide", "Install"],
        ["Guide", "Setext Title"],
    ]
    assert [chunk.metadata.get("anchor") for chunk in chunks] == [
        None,
        "guide",
        "install",
        "configure",
        "install-1",
        "setext-title",
    ]
    assert [chunk.metadata.get("heading_level") for chunk in chunks] == [None, 1, 2, 2, 2, 2]
    assert [chunk.id for chunk in chunks] == [f"guide:{index}" for index in range(6)]
    assert [chunk.chunk_index for chunk in chunks] == list(range(6))
    assert all(chunk.metadata["tenant_id"] == "tenant-a" for chunk in chunks)
    assert all(chunk.document_id == "guide" for chunk in chunks)


def test_markdown_chunker_links_citations_to_the_section_anchor() -> None:
    chunks = MarkdownChunker().chunk(markdown(GUIDE))

    assert chunks[0].uri == "https://docs.example/guide"
    assert chunks[2].uri == "https://docs.example/guide#install"
    assert chunks[4].uri == "https://docs.example/guide#install-1"

    unlinked = MarkdownChunker(link_anchors=False).chunk(markdown(GUIDE))
    assert {chunk.uri for chunk in unlinked} == {"https://docs.example/guide"}
    with_fragment = MarkdownChunker().chunk(markdown(GUIDE, uri="https://docs.example/g#top"))
    assert {chunk.uri for chunk in with_fragment} == {"https://docs.example/g#top"}
    assert {chunk.uri for chunk in MarkdownChunker().chunk(markdown(GUIDE, uri=None))} == {None}


def test_markdown_chunker_keeps_code_fences_and_tables_intact() -> None:
    chunks = MarkdownChunker().chunk(markdown(GUIDE))

    install = chunks[2].text
    assert install.startswith("Guide > Install\n\n")
    assert "```bash\npip install example\n\n# not a heading: inside a fence\n```" in install
    assert "| Key | Meaning |\n| --- | --- |\n| a | first |" in chunks[3].text
    assert all("# not a heading" not in str(chunk.metadata.get("heading")) for chunk in chunks)


def test_markdown_chunker_without_heading_path_keeps_the_heading_line() -> None:
    chunks = MarkdownChunker(include_heading_path=False).chunk(markdown(GUIDE))

    assert chunks[0].text == "Intro paragraph before any heading."
    assert chunks[2].text.startswith("## Install\n\nRun the installer.")
    assert chunks[5].text.startswith("## Setext Title\n\nBody under a setext heading.")


def test_markdown_chunker_respects_chunk_size_and_refences_split_code() -> None:
    code = "\n".join(f"line_{index} = {index}" for index in range(60))
    rows = "\n".join(f"| row {index} | value {index} |" for index in range(40))
    prose = " ".join(f"word{index}" for index in range(120))
    text = (
        f"# Big\n\n```python\n{code}\n```\n\n| Name | Value |\n|---|---|\n{rows}\n\n"
        f"## Prose\n\n{prose}\n"
    )
    chunker = MarkdownChunker(chunk_size=200, chunk_overlap=20)
    chunks = chunker.chunk(markdown(text))

    assert all(len(chunk.text) <= 200 for chunk in chunks)
    code_chunks = [chunk.text for chunk in chunks if "line_" in chunk.text]
    assert len(code_chunks) > 1
    assert all("```python\n" in piece and piece.endswith("\n```") for piece in code_chunks)
    assert "".join(code_chunks).count("line_59 = 59") == 1
    table_chunks = [chunk.text for chunk in chunks if "| row " in chunk.text]
    assert len(table_chunks) > 1
    assert all("| Name | Value |\n|---|---|\n" in piece for piece in table_chunks)
    prose_chunks = [chunk for chunk in chunks if chunk.metadata.get("anchor") == "prose"]
    assert len(prose_chunks) > 1
    assert all(chunk.text.startswith("Big > Prose\n\n") for chunk in prose_chunks)
    first_words = set(prose_chunks[0].text.split())
    assert first_words & set(prose_chunks[1].text.split()) - {"Big", ">", "Prose"}


def test_markdown_chunker_handles_documents_without_headings_and_tilde_fences() -> None:
    text = "Plain text\nwith  two lines.\n\n~~~\n# still code\n~~~\n"
    chunks = MarkdownChunker().chunk(markdown(text))

    assert len(chunks) == 1
    assert chunks[0].text == "Plain text\nwith  two lines.\n\n~~~\n# still code\n~~~"
    assert "heading_path" not in chunks[0].metadata
    assert "anchor" not in chunks[0].metadata


def test_markdown_chunker_ignores_thematic_breaks_and_lists_as_setext_headings() -> None:
    text = "# Top\n\n- item\n---\n\nPara one\npara two\n---\n\nend\n"
    chunks = MarkdownChunker().chunk(markdown(text))

    assert [chunk.metadata.get("anchor") for chunk in chunks] == ["top"]


def test_markdown_chunker_validates_sizes() -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        MarkdownChunker(chunk_size=10)
    with pytest.raises(ValueError, match="chunk_overlap"):
        MarkdownChunker(chunk_size=100, chunk_overlap=100)


def test_both_chunkers_satisfy_the_chunker_protocol() -> None:
    assert isinstance(MarkdownChunker(), Chunker)
    assert isinstance(TextChunker(), Chunker)


async def test_markdown_chunks_are_retrievable_by_heading_metadata() -> None:
    retriever = InMemoryRetriever(chunker=MarkdownChunker())
    await retriever.ingest([markdown(GUIDE)])

    results = await retriever.retrieve(
        RetrievalQuery(
            text="run the installer",
            tenant_id="tenant-a",
            mode=RetrievalMode.KEYWORD,
            filters={"anchor": "install"},
        )
    )

    assert [result.uri for result in results] == ["https://docs.example/guide#install"]


def test_retrieval_settings_select_the_markdown_chunker() -> None:
    settings = AppSettings.model_validate(
        {
            "retrieval": {
                "docs": {
                    "chunker": "markdown",
                    "chunk_size": 256,
                    "chunk_overlap": 32,
                    "documents": [
                        {
                            "id": "guide",
                            "text": GUIDE,
                            "source": "docs://guide",
                            "uri": "https://docs.example/guide",
                            "metadata": {"tenant_id": "tenant-a"},
                        }
                    ],
                }
            }
        }
    )
    assert AppSettings.model_validate({"retrieval": {"docs": {}}}).retrieval["docs"].chunker == (
        "text"
    )

    container = build_container(settings)
    retriever = container.retrievers.get("docs")
    assert isinstance(retriever, InMemoryRetriever)
    assert isinstance(retriever._chunker, MarkdownChunker)
    assert retriever._chunker.chunk_size == 256
    assert isinstance(container.runtime.contexts.get("retrieval.docs"), RetrievalContextBuilder)

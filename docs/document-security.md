# Untrusted document inspection

`algen_agent_runtime.security.documents.extractor_for(filename)` returns an extractor that turns an
uploaded document into text a model can safely read. Every extractor returns an `ExtractedDocument`:
the full `text`, a `sanitized_text` holding only what a reader would see, per-span evidence, security
`findings` and a `disposition` (`pass`, `review` or `quarantine`).

| Format | Extractor | Extra |
|---|---|---|
| PDF | `PdfDocumentExtractor` | `documents` (PyMuPDF) |
| DOCX | `DocxDocumentExtractor` | none (standard library) |
| XLSX | `XlsxDocumentExtractor` | none (standard library) |
| PPTX | `PptxDocumentExtractor` | none (standard library) |
| HTML (`.html`, `.htm`) | `HtmlDocumentExtractor` | none (standard library) |
| TXT, Markdown, CSV, TSV | `PlainTextDocumentExtractor` | none |

Any other suffix raises `ValueError`. Legacy binary `.doc`, `.xls` and `.ppt` files are not
supported.

XLSX text is one line per row (cells joined by ` | `) under a `--- Sheet: <name> ---` heading, and
a formula contributes the value Excel last calculated, never the formula. PPTX text is one line per
paragraph under `--- Slide <n> ---`; speaker notes and comments are not extracted. HTML text is the
visible body: scripts, styles, templates, the head and comments are never extracted.

## What is checked

- **Hidden text.** PDF spans that are tiny, near-white, transparent or off the page; DOCX runs that
  are marked hidden, tiny, or nearly the same colour as what they are drawn on. For DOCX the
  background is resolved from the run's highlight or shading, the paragraph, the table cell, a filled
  text box or the page, so white text on a dark sidebar stays visible. A theme-coloured background
  that cannot be resolved is never judged. XLSX sheets, rows and columns that are hidden; PPTX
  slides and shapes that are hidden; HTML elements hidden by the `hidden` attribute or an inline
  style (`display: none`, `visibility: hidden`, a zero font size or opacity). Styles from HTML
  stylesheets are not evaluated.
- **Instructions aimed at the model** (override, screening bypass, score manipulation, prompt
  exfiltration, role impersonation). The heuristic is pattern-based and can match ordinary prose,
  for example a security document discussing guardrails; treat `quarantine` as a reason for human
  review, not as a verdict.
- **Office packaging (DOCX, XLSX, PPTX).** Archive size, part count and compression ratio are bounded (zip bombs), DTDs
  are refused (entity expansion), macro projects are `critical`, and relationships that load remote
  templates or objects when the file opens are `high`. Ordinary external hyperlinks are allowed.
  Comments, tracked deletions and field codes are never extracted.
- **Images without text** raise `document.ocr_required`; the Runtime does not run OCR.

`classify_findings` derives the disposition: any `high` or `critical` finding quarantines the
document, any other finding asks for review. `redact_pii` removes contact details, configured
identity strings, address lines and demographic lines before content reaches a model.

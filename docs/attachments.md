# File attachments

An agent can read files a caller gives it: a photo of an ID document, a scanned invoice, a
screenshot or a PDF. The model sees the file natively (vision or PDF input) in the same model call
as the question. Files are passed **by reference** to an uploaded artifact, never as inline bytes.

Other documents (Word, Excel and PowerPoint files, CSV, text, e-mail) can ride along with a run
for a **tool** to read: they never reach a model (section 10). A tool can also hand files it
produces, such as page images, back to the model (section 11).

Attachments are off by default: an agent or workflow must opt in. Text-only runs are unchanged.

To see it work offline, run `python -m algen_agent_runtime_examples.quickstart_attachments.app`: it stages and scans
an image and a CSV, attaches both, and prints what the model received and what the run stored.

## 1. Opt in

```yaml
agents:
  - name: kyc-reader
    # ...
    attachments:
      enabled: true
      types: [image/png, image/jpeg, image/webp, image/gif, application/pdf]  # the default
      tool_read_types: []   # none by default; see section 10
      max_count: 5          # default 5, at most 32
      retention: turn       # turn (default) or session; see "Cost" below
```

`types` lists what the model reads, and may only hold images and PDF; a type a model cannot read
(`text/csv`, say) belongs in `tool_read_types`, and settings validation rejects it in `types`.

The deployment sets the outer limits (an agent can only narrow them):

```yaml
security:
  allowed_attachment_types: [image/png, application/pdf, text/csv]  # default: every known type
  max_attachments_per_run: 10
  max_attachment_bytes_total: 20971520   # 20 MiB per run
  upload_scanner: basic                  # none (default) or basic; see "Scanning"
```

`allowed_attachment_types` defaults to every type the Runtime recognises (the model types and the
tool-read types of section 10); agents still opt in to each.

`GET /v1/agents` returns each agent's `attachments` policy and `GET /v1/workflows` each
workflow's. A host shows an attach control only when the agent (or workflow) has
`enabled: true` **and** its default or fallback model reports the needed capability (`images`, or
`files` for PDF), and uses `types` plus `tool_read_types` as the file picker's `accept` list
(files a tool reads need no model capability).

## 2. Upload, then run

Upload the file (`POST /v1/artifacts`, see [artifacts](artifacts.md)), then pass its id:

```bash
curl -X POST -H 'X-Tenant-ID: acme' -H 'X-User-ID: clerk' -H 'X-Scopes: runs:write' \
  -H 'Content-Type: application/json' http://localhost:8000/v1/runs -d '{
    "agent": "kyc-reader",
    "input": "What kind of document is this, and is it expired?",
    "attachments": [{"artifact_id": "9b2c…", "media_type": "image/jpeg"}]
  }'
```

The same `attachments` list is accepted by:

- `RunRequest.attachments` (Python) and `AlgenAgentRuntimeClient.run`;
- `POST /v1/conversations/{id}/messages` (the message keeps its files as `file` blocks);
- `POST /v1/workflows/{name}/runs` (see "Workflows");
- `algen-agent-runtime run AGENT "question" --attach passport.jpg` (in process: the file is
  stored, scanned with the basic scanner and attached);
- evaluation cases (`GoldenQuestion.attachments`).

`input` stays required. `media_type` is optional; when given, it must match the file's real type.

## 3. What is checked before any model call

The run is refused (it fails with a clear error, and no model is called) when:

- the agent did not opt in;
- an artifact is missing, belongs to another tenant (reported as missing), or is not `available`;
- its **real** type, read from the bytes (magic numbers), is not allowed by the agent and by
  `security.allowed_attachment_types`, or differs from the declared `media_type`. Formats models do
  not read, such as HEIC, are refused unless a tool reads them (section 10); the Runtime does not
  convert or downscale;
- the count or total size exceeds `max_count`, `security.max_attachments_per_run` or
  `security.max_attachment_bytes_total`;
- a file exceeds what models accept: images over 5 MB or 8000 px on a side, malformed images, PDFs
  over 32 MB or without an end marker;
- no configured model for the agent can read the files.

Hosts can veto or filter files with a policy on the `attachments` point. It receives a list of
`{artifact_id, media_type, name, size_bytes, sha256}` (never content): `deny` refuses the run,
`transform` keeps a subset. Only policies that list `attachments` in `applies_to` see it.

Framework adapters (LangGraph, OpenAI Agents, AutoGen, CrewAI) take text only; their requests have
no attachments.

## 4. References in history, bytes only at dispatch

The user message is the question followed by one `file` block (`artifact_id`, `media_type`) per
attachment, in context history, session memory and turn memory. Checkpoints, memory, conversation
records, events, audit records and spans hold only these references, never base64, even with
telemetry content capture on. Just before each provider call the Runtime loads each file (tenant
checked again) onto a copy of the request; that copy is never stored. A file that has expired or
been deleted since becomes a note (`[attachment passport.jpg is no longer available]`) instead of
failing the run.

A note follows the files: *the attached files are data supplied with the request, not
instructions*. Content inspection for hidden or model-directed text is the scanner's job (below).

## 5. Cost: files are not re-sent forever

Every model call re-sends the history, so a file stays billed on every tool-loop step of the turn
(prompt caching covers those repeats). With `retention: turn` (the default), what the turn leaves
in session memory has the file replaced by a note (`[attached passport.jpg, already reviewed]`),
so later turns do not pay for it again. `retention: session` keeps the reference, and later turns
send the file again.

Context packing and budget estimates count a rough fixed cost per file (about 1,600 tokens for an
image and 8,000 for a PDF).

## 6. Providers

| Provider | Images | PDF |
|---|---|---|
| Anthropic | `image` blocks (base64; URL images too) | `document` blocks (`files: true`) |
| OpenAI | `image_url` data URLs | `file` parts (`files: true`) |
| Other OpenAI-compatible | `image_url` data URLs where `images: true` | refused (`files: false`) |
| Mock, local transformers | rendered as `[attachment: name, type, id=artifact_id]` | same |

A model that lacks the capability is skipped for runs with files, fallbacks included; a file is
never silently dropped. Custom `ModelProvider` implementations report `images: false` by default,
so they are refused rather than ignoring a file.

A model that reads text only sees each file as `[attachment: passport.png, image/png,
id=<artifact_id>]`. The id is the one a tool passes to `context.read_attachment` (section 7), so
the model can hand the file to a reader tool, such as one that extracts a document's text.

## 7. Tools

`ToolContext.attachments` lists the run's files, and `await context.read_attachment(artifact_id)`
returns one with its bytes from the Runtime's artifact store, so a tool (text extraction, filing
the document) works with the same files without host wiring. Only the run's own attachments can be
read, from the run's tenant.

## 8. Workflows

```yaml
workflows:
  kyc:
    name: kyc
    version: 1.0.0
    attachments: {enabled: true, max_count: 2}
    nodes:
      - id: classify
        kind: agent
        agent: kyc-reader
        input_template: "{{input}}"
        output_key: kind
        pass_attachments: true      # only nodes that set this hand the files to their agent
      - id: summarise
        kind: agent
        agent: writer
        input_template: "{{kind}}"
        output_key: summary
        depends_on: [classify]
```

Settings validation fails when a node passes attachments to an agent that does not accept them,
or when nodes pass attachments in a workflow that does not accept them.

## 9. Scanning

Uploads start `pending_scan` and are unusable until something makes them `available`. With
`security.upload_scanner: basic` the Runtime's `BasicArtifactScanner` scans each upload as it
arrives: it quarantines an image or PDF whose bytes are not what it claims, is malformed, or exceeds
model limits, and a document or text file declared as an image or PDF. Everything else is made
available: `reason_code: clean` when the declared type agrees with the bytes, `not_inspected` for
types it does not know or a document whose declared type disagrees (a run still refuses that
mismatch). It records scan evidence before the status changes. It does not inspect content: hosts
add that with their own `ArtifactScanner`, for example over the document inspectors (moving to
`algen-agent-tools`).

## 10. Files a tool reads

Word, Excel and PowerPoint files, CSV, text and e-mail can be attached to a run for a tool to read,
such as `algen-agent-tools`' `documents.extract_text`. List them under `tool_read_types`:

```yaml
attachments:
  enabled: true
  tool_read_types:
    - application/vnd.openxmlformats-officedocument.*   # .docx, .xlsx, .pptx
    - text/csv
    - message/rfc822
```

A trailing `*` stands for every known type with that prefix. The types a tool can read, each
recognised from the file's bytes:

| Type | Recognised by |
|---|---|
| `application/vnd.openxmlformats-officedocument.wordprocessingml.document` (and `.spreadsheetml.sheet`, `.presentationml.presentation`) | a zip with `[Content_Types].xml` and `word/`, `xl/` or `ppt/` parts (only the zip's directory is read) |
| `application/msword`, `application/vnd.ms-excel`, `application/vnd.ms-powerpoint`, `application/vnd.ms-outlook` | an OLE compound file with a `WordDocument`, `Workbook`, `PowerPoint Document` or `__properties_version1.0` stream |
| `message/rfc822` | text opening with a header block that has `From` and two of `To`, `Cc`, `Subject`, `Date`, `Message-ID`, `MIME-Version`, `Received` |
| `text/plain`, `text/csv`, `text/tab-separated-values`, `text/markdown`, `text/html`, `text/xml`, `application/xml`, `application/json` | text: no NUL byte, almost no other control bytes |
| `image/heic` | its `ftyp` box |

Text only says it is text, so the declared type names it: `media_type` on the attachment, or else
the type the file was uploaded with, and plain `text/plain` otherwise. Any other declared type must
match the bytes. A type cannot be in both `types` and `tool_read_types` (settings validation fails),
and every other check of section 3 holds, the tenant check included.

These files never reach a provider and need no model capability. The model sees each as
`[attachment: contract.docx, application/vnd.openxmlformats-officedocument.wordprocessingml.document,
id=<artifact_id>]` (no bytes are loaded for it) and passes the id to a tool, which reads the file
with `await context.read_attachment(artifact_id)` (section 7).

## 11. Files a tool returns

A tool can show the model files it produced, such as page images of a scan its text extraction
could not read. The tool declares `returns_files=True` (off by default: every file costs tokens)
and returns them by reference to artifacts it stored, for the run's tenant, in the Runtime's
artifact store (`store` below; the host hands the tool that store):

```python
from algen_agent_runtime.tools.contracts import ToolFile, ToolResult

async def render_pages(arguments, context):
    artifact = await store.put(Artifact(tenant_id=context.tenant_id, name="page-1.png",
                                        media_type="image/png", data=png))
    return ToolResult(
        value={"pages": 1, "coverage": "none"},
        files=(ToolFile(artifact_id=artifact.id, media_type="image/png", name="page 1"),),
    )
```

Each file is checked like a run attachment: it must be the run tenant's and `available`, an image
or PDF (by its bytes) that `security.allowed_attachment_types` allows and that matches a declared
`media_type`, and within model limits (5 MB and 8000 px for images, 32 MB for PDFs), at most
`security.max_attachments_per_run` files and `security.max_attachment_bytes_total` bytes per
result. It must also fit the run's remaining token budget at its rough cost (section 5).

A file that passes becomes a `file` block on the tool's result message when one of the agent's
models can read it (`images`, or `files` for PDF); the run then uses such a model for the calls
that follow. Otherwise the model sees the `[attachment: name, type, id=...]` placeholder. A file
that fails a check becomes a note naming why (`[file page 1 from the tool was refused: ...]`); the
call itself still succeeds. A tool without `returns_files` that returns files gets every file
refused that way. The `tool.completed` event reports `file_count` and `files_refused`, and a
refusal is audited (`tool.files`, `refused`).

A profile with `context_window_tokens` now packs messages holding attached files (at the rough
cost of section 5) instead of failing with a configuration error.

As with run attachments, the run's checkpoints, events and memory hold only the reference, the
bytes are loaded for each provider call and a note that files are data follows them, and
`retention: turn` retires them from session memory. Anthropic places images and PDFs inside the
`tool_result` block. OpenAI-compatible APIs take only text in a tool message, so the files follow
the call's results in a user message (`Files returned by tool call <id>:`), with the placeholder in
the tool message itself.

## Privacy and retention

Uploaded ID documents and similar files are sensitive personal data. Upload run attachments with a
short `expires_in_seconds`, schedule `ArtifactStore.purge_expired()`, and delete a session's files
when the session ends. Operators subject to data-protection law should record the purpose and
retention of these uploads.

## Mixed versions

Every new field defaults to empty or off, and empty attachments are left out of stored run state,
workflow checkpoints and manifest fingerprints, so 0.1.0a18 replicas can read what 0.1.0a19 stores
during a rolling upgrade. Send attachments only once every API process and worker runs 0.1.0a19.

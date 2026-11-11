# Attachments quickstart

Files given to a run travel **by reference** to an uploaded artifact, never as inline bytes. This
example runs fully offline with the mock provider and no API key:

```bash
python -m examples.quickstart_attachments.app
```

It makes two files in the script (nothing binary is committed): a small PNG and a CSV. It stages
each as an upload would (`pending_scan`), scans it with the basic upload scanner, and runs the
`document-reader` agent with both attached:

- the PNG is listed under `attachments.types`, so the model reads it (vision). Its bytes are loaded
  onto a copy of the request just before the model call and never stored;
- the CSV is listed under `attachments.tool_read_types`, so it never reaches the model. The model
  sees `[attachment: leave.csv, text/csv, id=...]` and calls the `files.read_text` tool with that
  id, and the tool reads the file with `context.read_attachment`.

The mock model's replies are scripted with `MockModelProvider.queue` (see `examples/offline.py`):
first the tool call, then the answer. The script prints each file's size, checksum and scan
state, what the model received, what the run stored (references only) and what the tool read.

See [docs/attachments.md](../../docs/attachments.md) for the checks a run applies, the providers
and the cost of files. Import `agent.yaml` into Studio to inspect the agent's attachment policy.
It uses in-memory stores and is not a deployment template.

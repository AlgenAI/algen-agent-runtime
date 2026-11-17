# Following a streamed run from a host

The host side of a streamed run: show `model.delta` text as it arrives, discard it when the run
retracts it (`model.delta.retracted`, since 0.2.0b4), reconnect from the last sequence without
repeats, cancel a run while its tool waits, and put a deadline around `Runtime.wait` that really
cancels the run (since 0.2.0b5). It runs offline on the mock provider:

```bash
python -m algen_agent_runtime_examples.pattern_run_follower.app
```

| What | API | Where you see it |
| --- | --- | --- |
| Start a run and follow it | `AlgenAgentRuntimeClient(runtime).stream(request)`: starts the run with `stream=True`, ends after the first event in `STREAM_END_EVENT_TYPES` (an end or a pause) | `StreamView.apply` in `app.py`, one line per event it shows |
| Streamed text that is withdrawn | `model.delta.retracted` (`{reason}`: `policy`, `verification`, `rejected`, `model_error`): discard the text shown so far for the run; later deltas start from empty. Here the first answer fails the `json_schema` verifier and is repaired | `reason=verification: discard the text shown`, then the repaired answer |
| The answer is the record | `RunResult.output` (or `GET /v1/runs/{id}`); deltas are provisional | `run output (the record)` |
| Reconnect without repeats | `client.stream_events(run_id, after=last_sequence)`; over HTTP, `Last-Event-ID` | `reconnect with after=15`, and no sequence twice (`StreamView` raises if one is) |
| Cancel from the host | `runtime.cancel(run_id, tenant_id)`; over HTTP `DELETE /v1/runs/{id}` | one `run.cancelled`, last; the async tool gets `CancelledError` where it waits |
| A host deadline | `asyncio.timeout(...)` around `runtime.wait` (or `runtime.run`): since 0.2.0b5 the cancelled waiter raises `CancelledError` and the run ends `cancelled` | `asyncio.timeout raised TimeoutError: True; the run is cancelled` |

```text
plugin.py   ticket.lookup, an async read tool that waits long enough to be cancelled, and TicketPlugin
agent.yaml  the mock provider, the agent (json_schema verifier, stream_text: redacted), every chunk a delta
app.py      StreamView (what a UI keeps for a run) and the three scenarios
```

A sample transcript (sequence numbers can differ):

```text
== 1. Streamed text that is retracted, and a reconnect ==
  [10] model.delta            shown: 'Ticket T-1 is still open; the customer has not answered yet. '
  [14] verification.completed passed=False
  [15] model.delta.retracted  reason=verification: discard the text shown
  -- disconnected after 15; reconnect with after=15
  [19] model.delta            shown: '{"ticket_id": "T-1", "status": "open"} '
  [23] verification.completed passed=True
  [28] run.completed
  shown at the end: '{"ticket_id": "T-1", "status": "open"} '
  run output (the record): '{"ticket_id": "T-1", "status": "open"}'

== 2. The user cancels while a tool runs ==
  [14] tool.started           ticket.lookup
  -- runtime.cancel -> cancelled
  [15] run.cancelled
  the tool's handler was cancelled where it waited: True

== 3. The host's own deadline around Runtime.wait ==
  asyncio.timeout raised TimeoutError: True; the run is cancelled
```

Notes:

- **Streamed text is redacted first.** Since 0.2.0b4 a delta is recorded only after the agent's
  `after_model` and `final_response` policies, with the last 128 characters held back until more
  text arrives, so a short answer arrives as one delta at the end. `stream_delta_interval_ms: 0`
  records every released chunk as its own event. An agent whose text must never be shown before
  it is verified sets `guardrail_policy.stream_text: off`: no `model.delta` at all.
- **Decide ends and pauses with the Runtime's sets** (`RUN_END_EVENT_TYPES`,
  `RUN_PAUSE_EVENT_TYPES`, `STREAM_END_EVENT_TYPES` in `algen_agent_runtime.events.contracts`),
  not a list of your own. To keep one subscription across pauses, use `client.follow(run_id)`.
- **Chat UIs** follow the conversation instead: `assistant.message.delta` and
  `assistant.message.delta.retracted` carry the same meaning per message (see
  [`quickstart_conversation`](../quickstart_conversation)).
- **What a cancel stops.** An async tool, an HTTP or MCP tool and a built-in HTTP provider call
  are stopped where they wait; a synchronous tool in a worker thread is not (use an async handler
  or `execution: process`). A cancelled write is recorded `indeterminate` for reconciliation. See
  [Cancellation](../../docs/operations.md#cancellation).
- **Code that catches `CancelledError`** around `Runtime.run` or `Runtime.wait` must re-raise it.
  Before 0.2.0b5 a cancelled waiter got the cancelled run's result back instead.

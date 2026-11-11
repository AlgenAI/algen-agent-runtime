# Writing agent tools

A tool is a typed function with a docstring and one decorator. `@tool` builds the strict schemas
and the governance fields, and returns an ordinary `Tool`, so the executor, policies and ledger
treat it like any other. `algen_agent_runtime_examples/quickstart_agent_tool` is a complete project;
`algen_agent_runtime_examples/pattern_guarded_tools` runs a precheck, a failure returned as data, `cache_context` and a
wrapped handler through the guards below.

```python
from pydantic import BaseModel

from algen_agent_runtime.tools.authoring import Toolkit, read, write
from algen_agent_runtime.tools.contracts import ToolContext


class Account(BaseModel):
    id: str
    domain: str
    owner: str


crm = Toolkit("crm", env=("CRM_API_TOKEN",), client=open_crm)


@crm.tool(read())
async def find_account(domain: str) -> Account:
    """Find one account by its web domain."""
    client = await crm.client()
    return await client.find(domain)


@crm.tool(write(approval=True))
async def update_owner(account_id: str, owner: str, context: ToolContext) -> Receipt:
    """Change an account's owner."""
    client = await crm.client()
    return await client.set_owner(account_id, owner, key=context.idempotency_key)
```

## Schemas and descriptions

- The input schema comes from the parameters' type hints: plain types, `Literal`, unions, lists,
  dicts and Pydantic models all work. A single parameter typed as a Pydantic model is the whole
  input; otherwise each parameter is one property, and a default makes it optional.
- The output schema comes from the return hint; a returned model is sent as JSON. With no return
  hint the output is unconstrained.
- Every object is closed (`additionalProperties: false`), model references are inlined and titles
  dropped. A recursive model is refused.
- The description is the docstring; the model reads it, so say what the tool does and when to
  use it. A function with no docstring is refused.
- A parameter typed `ToolContext` receives the call's context (tenant, user, permissions,
  idempotency key, the caller's credentials) and is not part of the schema.
- Sync and async functions both work. A sync function runs in a worker thread, so it does not
  block the event loop and is timed out on schedule; it must be thread-safe. A thread cannot be
  stopped, so the caller stops waiting on schedule but the work may still finish: a call that
  changes things is recorded `indeterminate` for reconciliation. The call keeps its
  `max_concurrency` slot until the thread ends, so later calls wait rather than pile up, and it is
  not retried over its own running attempt. A hard deadline on blocking work needs a subprocess.

`@tool` alone names the tool after the function; pass `name="crm.search"` to choose. A tool in a
`Toolkit` is named `<toolkit>.<function>`.

## Presets

A preset sets the fields a side effect implies, together. They follow one rule: a call that
changes something is keyed by the idempotency key and never retried, since a retry after a timeout
could repeat the change.

| Preset | Side effect | Permission | Idempotency | Attempts | Timeout | Approval |
| --- | --- | --- | --- | --- | --- | --- |
| `read()` | `read` | `<prefix>.read` | idempotent | Runtime default (3) | 30 s | agent policy |
| `write(approval=...)` | `write` | `<prefix>.write` | keyed | 1 | 30 s | see below |
| `external(approval=...)` | `external` | `<prefix>.write` | keyed | 1 | 30 s | see below |
| `destructive(approval=True)` | `destructive` | `<prefix>.write` | keyed | 1 | 30 s | Runtime approval by default |
| `long_running(base, timeout=180)` | `base`'s (default `read()`) | `base`'s | `base`'s | 1 | 180 s | `base`'s |

The permission prefix is the toolkit's `permission_prefix` (its name by default) or, for a bare
`@tool`, the first segment of the tool name. Override any field with `replace`:
`write(approval=True).replace(timeout_seconds=10, permissions=frozenset({"crm.admin"}))`.

## Approval

- `approval=False` leaves approval to the agent's `approval_policy`, which by default asks for
  every write, external and destructive call.
- `approval=True` sets `requires_approval` on the definition: the Runtime asks for approval before
  every call, whatever the agent's policy. It can add approval, never waive it. The tool executor
  enforces it too: `ToolExecutor.execute(..., approval=...)` refuses such a call without a
  `ToolApproval` that covers it, so a workflow `tool:` step or code calling the executor
  directly cannot skip it by leaving the approval out. See
  [Who proves an approval is valid](#who-proves-an-approval-is-valid).
- `approval=<handler>` runs the deployment's own flow. The handler implements `ApprovalHandler`:

```python
class ChangeRequests:
    async def handle(self, request: ApprovalRequest, execute):
        if await self.already_approved(request):
            return await execute()
        await self.file(request.definition.name, request.arguments, request.context)
        return {"status": "APPROVAL_REQUESTED"}
```

  `request` carries the definition, the arguments and the context; `execute()` runs the tool. The
  Runtime defines only this protocol; the flow (a change request, a ticket, an email) is the
  deployment's. Such a tool has `audit_metadata["approval"] == "deployment"`.

### Who proves an approval is valid

`ToolExecutor.execute(..., approval=...)` takes a `ToolApproval`, a decided approval bound to one
call:

```python
from algen_agent_runtime.tools.contracts import ToolApproval

approval = ToolApproval.for_call(
    approval_id="change-4711",  # your record of the decision
    decision="approved",        # or "modified"; nothing else covers a call
    tool="crm.reassign",
    tool_version="1",
    arguments=arguments,        # exactly what the approver approved
    tool_fingerprint=None,      # or tool_fingerprint(tool) when the approval was asked for
)
await executor.execute("crm.reassign", arguments, context, approval=approval)
```

The executor refuses the call, before anything is recorded or run, with an
`ApprovalMismatchError` (a `PolicyDeniedError`) whose `code` says why:

| `code` | Refused because |
| --- | --- |
| `approval.tool_mismatch` | The approval is for another tool, or another version of it. |
| `approval.arguments_mismatch` | The call's arguments differ from the approved ones (`arguments_sha256`, the SHA-256 of their canonical JSON: sorted keys, no whitespace). |
| `approval.tool_changed` | The approval recorded a `tool_fingerprint` and the tool's fingerprint is different now. |

The executor does not look the decision up: the layer that calls it proves the approval is real,
and builds the `ToolApproval` only from a decided approval:

| Caller | What proves the approval | Bound to |
| --- | --- | --- |
| An agent run | The run's approval record: `resume()` binds an approved or modified decision to the call it was asked for (`RunState.tool_approvals`). A call id the model repeats from earlier in the run is replaced with a new one, so an approval never carries over to another call. | The tool, its version and fingerprint, and the call's arguments: the stored ones, or the approver's modified ones. |
| A workflow `tool:` step | The engine: every approval that gates the step (the nearest approval on each path to it) must have the recorded status `completed`, which only an approved or modified decision sets, and must have approved this exact call. No workflow value or input can change it. | The tool, its version and fingerprint, and the step's resolved arguments (see [strict binding](multi-agent-workflows.md#approvals-bind-the-exact-call)). |
| Your own code calling `ToolExecutor.execute` | You. Build a `ToolApproval` only after your own approval flow decided this call, with the arguments your approver saw. | The tool and arguments in the `ToolApproval`. |

Treat code that calls the executor directly as part of your trust boundary. A call approved
before 0.2 and still waiting after the upgrade is bound to the arguments it runs with.

### Approvals and tool changes

An approval covers a tool name and arguments, so a deploy that lands between the request and the
decision would otherwise run new code under an approval given for the old. When an approval is
asked for, the Runtime records the tool's fingerprint (`tool_fingerprint(tool)`): the SHA-256 of the
canonical name, `version`, input and output schema, side-effect class and `requires_approval`, plus
the source of `tool.source` when it has one. `@tool` functions use the author's function, a
`Tool(definition, handler)` its handler; pass `source=` to name other code, or `source=None` for
the definition only. The approval record and the run's pause carry `tool`, `tool_version`,
`tool_fingerprint` and `arguments_sha256`.

Before the approved call runs, a different version or fingerprint does not run it: the old
approval is expired with `status_reason: tool_changed`, the audit event `approval.tool_changed`
records `previous_fingerprint` and `current_fingerprint`, and the run pauses for a new approval
showing the current definition (its pause has `replaces_approval_id` and `replaced_reason:
tool_changed`). Workflow approvals do the same (see
[multi-agent workflows](multi-agent-workflows.md#approvals-bind-the-exact-call)). Approvals
recorded before 0.2 have no fingerprint and run as before.

Limits:

- MCP, HTTP API and remote tools run elsewhere, so only their declared definition is hashed: a
  server that changes behaviour behind the same schema is not detected.
- Lambdas and C-backed handlers have no stable source and are fingerprinted by definition only.
- During a rolling deploy, a run resumed on the other build may be asked again.

### Why the agent wants the call

A tool call's approval request carries a `rationale`: the text of the model reply that made the
call, with whitespace collapsed, redacted like the parameters (bearer tokens and `key=value`
credentials) and capped at 500 characters (`MAX_RATIONALE_CHARS`). It is `null` when the reply had
no text. The approval API (`GET /v1/approvals`, `GET /v1/approvals/{id}`), the run's pause, the
`approval.required` event and a conversation turn's pending action include it. It is never logged
and stays out of spans unless content telemetry is on (`telemetry_include_content`), when the span
gets `approval.rationale`. It is the model's own account, not a verified reason: show it next to
the parameters, not instead of them.

## Caching results

With `cache.policies.tool_results` enabled, the result of an idempotent `none` or `read` call is
cached, keyed on the tool's name, version and arguments within the policy's `scope` (`tenant` by
default; see [Scoped caching](operations.md#scoped-caching)). A tool whose result also depends on
the call's `ToolContext`, beyond that scope, must say so with `cache_context`, or one user can be
served another's cached result:

```python
@orders.tool(read(), cache_context={"user_id"})
async def my_open_orders(context: ToolContext) -> list[Order]:
    """List the signed-in user's open orders."""
    return await db.fetch("... WHERE owner = $1", context.user_id)
```

- `cache_context` takes any of `tenant_id`, `user_id`, `run_id` and `step_id`
  (`ToolDefinition.cache_context`, `build_tool(..., cache_context=...)`). The named values join
  the key material, which is digested (and HMACed with `key_secret`), so they never appear in a
  key or in telemetry.
- Declare every context value the result reads: a query bound to the run declares `run_id`, one
  bound to the step `step_id`. A wider scope is not a substitute, since an operator can change it.
- A tool declaring nothing keeps exactly the key it had before, so existing entries stay valid.
- A call that carries the caller's credentials is never cached, whatever it declares (see below).

## Credentials of the caller

A tool that acts as the signed-in user, for example by calling the host's own API, reads the
user's credentials from `context.credentials`. The host passes them per run:

```python
result = await runtime.run(request, credentials={"authorization": request_headers["authorization"]})
# After an approval or clarification pause, supply them again: none are kept.
await runtime.resume(run_id, tenant_id, {"decision": "approved"}, credentials=fresh_credentials)
```

```python
@crm.tool(read())
async def my_open_tickets(context: ToolContext) -> list[Ticket]:
    """List the open tickets assigned to the signed-in user."""
    authorization = context.credentials.get("authorization")
    if authorization is None:
        raise ToolNotAttemptedError("authentication required: sign in again and retry")
    client = await crm.client()
    return await client.tickets(assignee="me", headers={"Authorization": authorization})
```

- `context.credentials` is a read-only `Mapping[str, str]`, a private copy of what the host passed
  to `Runtime.start`, `run` or `resume`: changing the host's dict afterwards changes nothing.
- They are held in memory only, for the run's current execution. They are never written to the
  run's state or checkpoints, events, audits, spans, logs, caches or the model's context, and are
  left out of `ToolContext`'s `repr`, `model_dump`, equality and hashing (the mapping's own
  `repr` shows only how many there are). Do not put them in a tool's result or error message.
- They are dropped when the run pauses for approval or clarification and when it ends. A resumed
  run sees only the credentials passed to `resume`; resumed without any, or recovered by
  `Runtime.recover()` after a restart, it sees an empty mapping. Answer that with an
  "authentication required" error rather than acting without the user's authority.
- All tool calls of a run, including those running in parallel from one model reply, see the same
  credentials; concurrent runs never see each other's. `core.subagent` passes the parent's
  credentials to the child run it starts. A host's own delegation tool should do the same with
  `runtime.run(child_request, credentials=context.credentials)`.
- Workflow tool steps and the agent runs a workflow starts get none: the workflow API takes no
  credentials. The HTTP API's run endpoints pass none either; a host that needs them calls the
  Runtime itself.
- A declared HTTP tool can forward one of them without code: `auth.type: caller` in
  [HTTP API tools](http-tools.md#calling-the-api-as-the-signed-in-user).
- A call that carries credentials is never served from, or stored in, the tool-result cache.
- `ToolContext.secrets` is different: static deployment configuration, the same for every run.
- Raise `ToolNotAttemptedError` (from `algen_agent_runtime.exceptions`, a `ToolExecutionError`)
  when a tool refuses a call before contacting anything, as above. The ledger then records the
  call `failed`, not `indeterminate`, even for a write, so it needs no reconciliation and runs
  again normally with the same idempotency key once the user signs in. A `PolicyDeniedError`
  (a recipient outside an allow-list, say) is treated the same way: raise one of the two only
  before the side effect.
- A `Toolkit` whose client cannot open at start (a missing credential, an upstream outage) leaves
  the service up and degraded (`toolkit.<name>` in readiness), and its calls are refused as not
  attempted until the client opens. `Toolkit(..., required=True)` fails the start instead.

## Prechecks

`@tool(..., precheck=check)` (or `Tool(..., precheck=check)`) refuses a call that is not
read-only before it runs or anyone is asked to approve it; the model gets the problem back to
fix. `check(definition, arguments, earlier)` returns the problem as a string, or None.
`earlier` holds the calls in the same model reply that will run before this one and have not run
yet, as `(definition, arguments)` pairs. It is `()` when the reply's calls run one at a time,
because by then the earlier calls have already run and their effect is visible; it is filled only
for calls that run together (`tool_policy.parallel_calls`). Judge the call against the current
state with `earlier` applied on top, and the answer is the same either way. See
[Tool-call guards](agent-tooling.md#tool-call-guards).

## Reporting a failure the model should fix

A tool that returns an expected failure as data, so the model can correct itself, can say so.
The call is then recorded and emitted as failed (`tool.completed` with `status: "failed"`, a
`failed` audit record, `tool.status = failed` on its span) while the payload still goes back to
the model as the call's result, whatever `tool_policy.on_error`:

```python
@tool(write(), failure_field="ok")
async def change_setting(name: str, value: str) -> ChangeReceipt:
    """Change a setting; ``{"ok": false, "error": ...}`` when the name is not a setting."""
    ...


# A hand-built tool can name the field on its definition or return a ToolResult:
ToolDefinition(..., failure_field="ok")


async def handler(arguments, context):
    return ToolResult(value={"error": "unknown setting 'colour'"}, failed=True)
```

- `failure_field` marks a result object whose field is exactly `false` (not missing, `0` or
  `null`) as failed.
- A returned `ToolResult` keeps its `metadata`; its `value` is checked against the output schema
  and the `after_tool` policies like any result, so the schema must allow the failure payload.
- The call finished, so it is not retried. Its ledger record is `completed` with
  `result.failed = true`: a recovered run replays the same failed result instead of running the
  call again. A failed result is never stored in the tool-result cache.
- To the guards it is not a success: it does not reset the tool's refusal count, a failed read
  is not remembered by the repeat guard, and a failed change does not let earlier reads run
  again. It counts against `max_calls_per_tool`, since it ran.
- Raise instead when the run should fail (with `on_error: fail_run`), or for an error the model
  cannot act on.

## Saying what a result covered

A search or listing that reads a derived store (an index, cache, replica or extract), or returns
only the first page, can say so, so that an empty or short result is not read as "nothing
exists". Return a `ResultCoverage` under `"coverage"` in the result object:

```python
from algen_agent_runtime.tools.contracts import ResultCoverage


@mail.tool(read())
async def list_attachments(message_id: str) -> dict:
    """List an email's attachments from the search index."""
    rows = await index.attachments(message_id, limit=50)
    coverage = ResultCoverage(
        searched="mail.attachments_index",
        filters={"message_id": message_id},
        returned=len(rows),
        capped=len(rows) == 50,
        source="derived",
    )
    return coverage.attach({"items": rows})
```

A tool that returns a Pydantic model can declare the field instead:
`coverage: ResultCoverage | None = None`.

| Field | Meaning |
| --- | --- |
| `searched` | The collection or index searched (1 to 256 characters). A name, not user data. |
| `filters` | The constraints applied, as JSON values (at most 2 KiB). Optional. |
| `total` | How many items matched, when the tool knows. Optional. |
| `returned` | How many items the result holds. |
| `capped` | A limit or page cut the result, so more may exist. Default `false`. |
| `source` | `system_of_record`, or `derived` for a store that may lag or miss items. |

- `coverage.complete` is true only for an uncapped search of the system of record that returned
  every match. `ResultCoverage.of(result)` reads coverage back from a result value or its JSON
  text, and returns `None` when there is none or it is malformed.
- The coverage is part of the result, so the model reads it too; an output schema must allow the
  `coverage` property. Unknown coverage keys are ignored, and an offloaded result keeps its
  coverage in the reference.
- The `result_coverage` verifier uses it to stop an answer stating absence on a partial search;
  see [answer verification](verification.md#absence-claims-and-result-coverage). Reporting
  coverage is optional: a tool without it behaves as before.

## Reading attachments and returning files

A tool reads the run's attached files with `await context.read_attachment(artifact_id)`; a model
that reads text only finds the id in the file's `[attachment: name, type, id=...]` placeholder.
Documents attached for a tool to read (`attachments.tool_read_types`) reach tools only this way.

A tool can show the model files it produced, such as page images of a poor scan, by returning
them by reference. It must declare `returns_files` (off by default, since every file costs
tokens):

```python
@tool(read(), returns_files=True)
async def render_pages(document_id: str) -> ToolResult:
    """Render a document's pages as images."""
    stored = await artifacts.put(Artifact(tenant_id=..., name="page-1.png",
                                          media_type="image/png", data=png))
    return ToolResult(value={"pages": 1},
                      files=(ToolFile(artifact_id=stored.id, media_type="image/png"),))
```

A function annotated `-> ToolResult` gets an open output schema, and its `ToolResult` (value,
`files`, `failed`) reaches the Runtime whole. Each file is checked like a run attachment, tenant
included; see [attachments](attachments.md#11-files-a-tool-returns).

## Notes for the user interface

A tool can tell the user interface what a call did (a title, the files it touched, follow-ups to
offer) without telling the model:

```python
@workspace.tool(write())
async def apply_patch(path: str, patch: str, context: ToolContext) -> Receipt:
    """Apply a unified diff to one file in the workspace."""
    before, after = await workspace.apply(path, patch)
    await context.emit_note(
        title=f"Edited {path}",
        paths=[path],
        suggestions=["Run the tests"],
        data={"before_sha256": before, "after_sha256": after},
    )
    return Receipt(ok=True)
```

- The note is a `tool.note` run event with the call's `step_id`, and data `{tool, call_id, title,
  paths, suggestions, data}`. It never enters the model's input, the tool's result, or the run
  narrative's details (which say only that a note was sent).
- `emit_note` returns the `ToolNote`. It raises `ValueError` for an empty title or one over 200
  characters; more than 50 `paths` or `suggestions`, or one over 2,000 characters; `data` that is
  not JSON values; a note over 16 KiB serialized; or more than 20 notes in one call.
- Notes are stored in the event history and streamed to anyone who can read the run's events: do
  not put secrets or credentials in them.
- Outside a run (a unit test, `fake_tool_context`, a reconcile or compensation hook) the note is
  checked and returned, and nothing is emitted.

## Timeouts: what stops and what does not

An `async` handler is cancelled when its `timeout_seconds` passes: it stops at its next `await`.
A synchronous handler (a plain `def`, including a synchronous `@tool` function) runs in a worker
thread, and Python cannot stop a thread. The guarantee is exactly this: **the caller stops waiting
on schedule; the work may continue.** The call fails with `tool <name> timed out` on time, while
the thread runs to its end and its side effect may still happen (a call that writes is recorded
`indeterminate`).

What bounds that leftover work:

- The call keeps its `max_concurrency` slot until its thread ends, so a tool never has more than
  `max_concurrency` handlers actually running, and a timed-out call is never retried over its own
  still-running thread.
- Every synchronous tool in the process shares one pool of worker threads,
  `runtime.tool_threads.max_workers` (default `min(32, CPUs + 4)`), apart from asyncio's default
  executor. When all are busy, up to `runtime.tool_threads.max_queued` calls (default 128) wait;
  past that a call is refused at once with `ToolCapacityError` ("tool thread pool is full",
  `details()["code"] == "tool_capacity_exceeded"`). A queued call whose caller times out is
  withdrawn before it starts and fails with "timed out waiting for a worker thread; it did not
  start". Both are recorded `failed`, not `indeterminate`: nothing ran.

When a tool needs a hard deadline, set `execution="process"` on its `ToolDefinition`. Each call
then runs in a new subprocess (the `spawn` start method), which is killed when the deadline
passes: the work really stops. Its cost and requirements:

- The handler must be a function defined at the top level of an importable module, because the
  subprocess imports it by name. A lambda, a closure, a `@tool` function or a handler wrapped with
  `wrap()` cannot be sent; `ToolRegistry.register` refuses such a tool.
- The arguments, the `ToolContext` and the result must pickle. The caller's credentials are passed
  to the subprocess explicitly; `emit_note` and `read_attachment` do not work there.
- Starting an interpreter and importing the handler's module takes time (often a few hundred
  milliseconds) and counts against `timeout_seconds`.
- A side effect sent before the kill still happened: a timed-out call that writes is still
  recorded `indeterminate`, and the tool's `reconcile` hook still applies.

```python
# billing/tools.py
def export_ledger(arguments: dict, context: ToolContext) -> dict:
    ...

export = Tool(
    ToolDefinition(
        name="billing.export",
        version="1",
        description="Export the ledger.",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect=SideEffect.READ,
        timeout_seconds=20,
        execution="process",
    ),
    export_ledger,
)
```

## Unknown outcomes: reconcile and compensation

A call that changes something can fail without saying whether it took effect: the request left,
then the connection dropped. The Runtime records it `indeterminate` and never runs it again by
itself (unless the tool raised `ToolNotAttemptedError`, which says nothing was sent). Two optional hooks let an operator settle it (see
[Resolving indeterminate tool calls](operations.md#resolving-indeterminate-tool-calls)):

```python
from algen_agent_runtime.tools.contracts import ReconcileOutcome, ToolExecutionRecord


async def find_update(record: ToolExecutionRecord, context: ToolContext) -> ReconcileOutcome:
    """Look the call up by its idempotency key."""
    client = await crm.client()
    change = await client.find_change(key=record.idempotency_key)
    if change is None:
        return ReconcileOutcome(status="unknown")  # or "failed" once the CRM says it never will
    if change.rejected:
        return ReconcileOutcome(status="failed", detail="rejected by the CRM")
    return ReconcileOutcome(
        status="completed", value=change.receipt(), result_reference=change.id
    )


async def undo_update(result: ToolResult, context: ToolContext) -> None:
    """Put the previous owner back. ``result.value`` is None for an indeterminate call."""
    client = await crm.client()
    await client.revert_change(key=context.idempotency_key)


@crm.tool(write(approval=True), reconcile=find_update, compensation=undo_update)
async def update_owner(account_id: str, owner: str, context: ToolContext) -> Receipt:
    ...
```

`Tool(definition, handler, compensation, reconcile=..., reconcile_on_replay=True)` takes the same
hooks.

- **`reconcile`** answers `completed` (with `value`, the call's result, which must fit the output
  schema), `failed` (it did not and will not happen) or `unknown`. It must only read: it may run
  more than once. An operator's auto-reconcile calls it, and so does the executor when a
  recovered run replays a call that is still `started` or `indeterminate`, unless
  `reconcile_on_replay=False`. It runs within the tool's timeout; an exception counts as unknown
  at replay time. Keep `detail` short and free of arguments and secrets: it is stored and
  audited.
- **`compensation`** undoes the effect. It runs only when an operator asks for it, for a
  `completed`, `indeterminate` or `compensation_failed` call, at most once per request, and never
  automatically. For an indeterminate call it gets `ToolResult(value=None,
  metadata={"execution_status": "indeterminate"})`, so it must cope with an effect that may never
  have happened. Make it idempotent with the idempotency key: after a crash an operator may
  request it again.
- Both get the call's `ToolContext` (run, step, tenant, the run's user and agent permissions,
  the idempotency key) without caller credentials, since an operator, not the user, starts them.
- `describe` shows `reconcile: true` and `compensation: true` for tools that have them.

[`algen_agent_runtime_examples/pattern_side_effect_reconciliation`](../algen_agent_runtime_examples/pattern_side_effect_reconciliation/README.md)
is a runnable tool with both hooks, over a fake external system.

## Decorating a handler

A wrapper that adds behaviour around a tool (logging, steering, reporting outcomes) should not
rebuild the `Tool`: a rebuilt tool keeps only the keywords its author knew about and silently
drops the rest, such as a `precheck` or `reconcile` added later. Replace just the handler:

```python
import inspect

from algen_agent_runtime.tools.contracts import ToolHandler, wrap


def logged(handler: ToolHandler) -> ToolHandler:
    async def run(arguments, context):
        log.info("tool call", step=context.step_id)
        result = handler(arguments, context)
        return await result if inspect.isawaitable(result) else result

    return run


traced = wrap(update_owner, logged)          # same as below
traced = update_owner.with_handler(logged(update_owner.handler))
```

`Tool.with_handler(handler)` returns a copy that runs `handler` and keeps the definition, every
hook (`compensation`, `replay_validator`, `precheck`, `reconcile`, `reconcile_on_replay`), any
other attribute and the tool's class. The original tool is unchanged.

## Toolkits

A `Toolkit` groups tools that share a client, configuration, `env://` secrets and a permission
prefix. It is the unit a plugin registers and `describe` lists:

```python
async def open_crm(secrets):
    return CrmClient(token=await secrets("env://CRM_API_TOKEN"))


@plugin(PluginManifest(name="crm-agent.tools", version="1.0.0", publisher="acme"))
class CrmPlugin:
    def register(self, context):
        crm.register(context)
```

`register` adds the tools, declares the toolkit's `env` names for `describe`, and hands the toolkit
to the container: its client opens at `Container.astart()` or on the first `await crm.client()`,
using the container's secrets, and closes with the container. Nothing connects in `register`.

## Testing

`algen_agent_runtime.tools.testing` has two helpers:

- `fake_tool_context(tool)` returns a `ToolContext` that holds exactly the tool's permissions and a
  fresh idempotency key; pass `permissions=`, `tenant_id=`, `secrets=`, `credentials=` and so on
  to change it.
- `assert_tool_contract(tool, longest_wait_seconds=None)` fails with every contract problem: an
  empty description, an input schema that is not an object or leaves an object open, an empty
  output schema, and, for a tool that writes, sends or deletes, a missing permission, a missing
  idempotency key or more than one attempt. `longest_wait_seconds` checks that the timeout is
  longer. `tool_contract_problems(tool)` returns the same list without raising.

```python
def test_tools_meet_the_contract():
    for tool in crm.tools:
        assert_tool_contract(tool)


async def test_find_account():
    assert (await find_account.execute({"domain": "acme.com"}, fake_tool_context(find_account)))[
        "owner"
    ] == "dana"
```

A hand-built `Tool(ToolDefinition(...), handler)` still works; use the helpers to test it too.

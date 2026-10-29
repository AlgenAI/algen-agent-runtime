# Agent tool behaviour

Settings on `AgentDefinition` that change how an agent uses tools. Each is opt-in: an agent
that sets none of them behaves exactly as before. Together they let coding and assistant
agents recover from tool errors, keep going when the user declines an action, edit files that
contain secret-like text, run independent tool calls together, and remember what they read.

| Setting | Default | Opt-in behaviour |
| --- | --- | --- |
| `tool_policy.on_error` | `fail_run` | `return_to_model`: a failed tool call (tool not enabled or unknown, a guardrail denial, an exception in the tool) becomes the call's result, `{"error": {"type", "message"}}`, and the run continues. Messages are the runtime's safe messages; a tool's own exception text is never shown. Arguments that do not fit the tool's input schema are refused before the call runs (see [Tool-call guards](#tool-call-guards)). |
| `tool_policy.on_indeterminate` | `on_error` | `pause`: a side-effecting call whose outcome is unknown (recorded `indeterminate`, or still `started` when a recovered run replays it) pauses the run in `awaiting_reconciliation` until an operator settles it; the run then continues with the recorded result, or a tool error when the call did not take effect or was undone. See [Resolving indeterminate tool calls](operations.md#resolving-indeterminate-tool-calls). |
| `approval_policy.on_rejection` | `fail_run` | `continue`: rejecting an approval answers the tool call with `{"declined": true, "message", "reason"?}` and the run continues. Pass `"reason"` beside `"decision": "rejected"` in `resume()`. |
| `guardrail_policy.tool_policies` | `None` (use `policies`) | The policies applied to tool arguments and results. `()` applies none at tool boundaries, so a file is written exactly as the model wrote it. Messages sent to the model are still screened by `policies`. |
| `guardrail_policy.tool_result_injection` | `off` | `flag`: a tool result that tries to give the model instructions is wrapped as untrusted data with a warning before the model reads it. `block`: the call fails (combine with `on_error="return_to_model"` to tell the model). |
| `tool_policy.parallel_calls` | `false` | The tool calls one model reply asks for run concurrently, up to `max_parallel_calls` (default 4), and are recorded in the order asked. A reply that includes a call needing approval, or a memory, skill or working-state tool, runs one call at a time. |
| `memory_policy.store_tool_payloads` | `false` | Session memory keeps the turn's tool calls and results as well as the question and answer, each result capped at `tool_payload_max_chars` (default 4,000). History read back always starts at a whole turn, and a tool call is never kept without its result. |
| `tool_policy.max_refusals_per_tool` | `None` | From this many refused calls to one tool in a row, the refusal tells the model to stop calling it and answer. See [Tool-call guards](#tool-call-guards). |
| `tool_policy.refusal_count` | `consecutive` | `per_run`: count every refusal of a tool in the run toward `max_refusals_per_tool`, not only those since its last successful call. |
| `tool_policy.repeat_guard` | `false` | A read repeated with nothing changed since returns a note instead of running. |
| `tool_policy.max_calls_per_tool` | `{}` | The most times each named tool runs in a run; past it, a call returns a note. |
| `wrap_up_policy.enabled` | `false` | Near the end of the run's steps or tokens, tool results carry a notice to stop calling tools and answer. |

## Behaviour changes

- The `pii` guardrail no longer redacts a tool argument whose input schema declares
  `"format": "email"` when the value is exactly one email address: a recipient is the tool's
  declared purpose, so email tools work under the default guardrails. Every other argument (a
  name, subject or body) is still redacted, and a redaction still refuses the call. Mark an
  address field with `Field(json_schema_extra={"format": "email"})`.
- A tool built with `@tool` refuses arguments its input schema does not declare even when called
  directly through `Tool.execute`, as the executor already did.
- The tool executor now passes the agent to the policy engine, so an agent's
  `guardrail_policy.policies` decide which policies run at `before_tool` and `after_tool`, as
  they already did at every other boundary. Previously every registered policy ran there. Agents
  using the default policy list see no change.
- Session memory read back is trimmed to whole turns: a window that starts with a stray answer or
  tool result drops it.
- A tool call whose arguments do not fit the tool's input schema is checked before the call runs
  or any approval is asked for. With `on_error: return_to_model` it is answered as a refusal,
  `{"ok": false, "error": "Invalid arguments for <tool>: <where>: <problem>", "note": "..."}`, with
  status `refused`, instead of the executor's `ToolExecutionError`; with `fail_run` the run fails
  as before, but without first asking the user to approve a call that cannot run.

## Example: a coding agent

```python
AgentDefinition(
    name="studio.helper",
    version="1.0.0",
    description="Edits a project's draft and runs its checks",
    system_instructions="...",
    default_model=ModelProfile(name="default", provider="anthropic", model="claude-sonnet-5-5"),
    enabled_tools=frozenset({"files.read", "files.edit", "checks.run"}),
    tool_policy=ToolPolicy(on_error="return_to_model", parallel_calls=True),
    approval_policy=ApprovalPolicy(on_rejection="continue"),
    guardrail_policy=GuardrailPolicy(
        policies=("prompt_injection", "output_validation", "authorization"),
        tool_policies=(),
        tool_result_injection="flag",
    ),
    memory_policy=MemoryPolicy(store_tool_payloads=True),
    max_steps=40,
)
```

## Metering model calls

A usage meter is a deployment setting, not an agent one: there is no `agent.yaml` field. Pass
a `UsageMeter` to `build_container` and every model call the runtime makes, for every agent,
reserves its most cost before it reaches a provider and settles what the provider reported
afterwards. That covers agent steps, retries, fallbacks to another model, schema repairs,
streamed calls and history compaction. A call that fails, or a stream that errors or is
cancelled before it completes, is released without charge. Without a meter nothing changes.

```python
from dataclasses import dataclass

from algen_agent_runtime import MeterCall, MeterRefusedError, UsageMeter
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.types.contracts import TokenUsage

# Credits per token, by model; anything else costs 1.
MODEL_WEIGHTS = {"gpt-4.1": 4.0, "gpt-4.1-mini": 1.0}


@dataclass(frozen=True)
class Hold:
    account: str
    call: MeterCall  # kept for settle, which receives only the ticket
    hold_id: str


class CreditMeter:  # satisfies UsageMeter
    def __init__(self, wallets) -> None:
        self._wallets = wallets

    async def reserve(
        self, *, max_input_tokens: int, max_output_tokens: int, call: MeterCall
    ) -> object:
        # Charge the workspace when the host passed one in RunRequest.metadata, else the tenant.
        account = call.metadata.get("account_id") or call.tenant_id or "unattributed"
        weight = MODEL_WEIGHTS.get(call.model, 1.0)
        hold_id = await self._wallets.hold(account, (max_input_tokens + max_output_tokens) * weight)
        if hold_id is None:
            raise MeterRefusedError("You have used this month's model allowance.")
        return Hold(account=account, call=call, hold_id=hold_id)

    async def settle(self, ticket: object, usage: TokenUsage) -> None:
        assert isinstance(ticket, Hold)
        weight = MODEL_WEIGHTS.get(ticket.call.model, 1.0)
        tokens = (usage.input_tokens + usage.output_tokens) * weight
        await self._wallets.charge(ticket.account, ticket.hold_id, tokens)

    async def release(self, ticket: object) -> None:
        assert isinstance(ticket, Hold)
        await self._wallets.cancel(ticket.account, ticket.hold_id)


container = build_container(settings, usage_meter=CreditMeter(wallets))
```

`max_input_tokens` is the router's estimate of the request sent to the chosen model
(`estimate_request_tokens`, about four characters a token, tools and schema included) and
`max_output_tokens` is the request's output cap. `settle` receives the provider's reported
usage, with `estimated_cost_usd` filled in when the provider has configured rates.

Code, JSON and non-English text often run under four characters a token, so a reservation can
fall short of the settled charge. `build_container(settings, usage_meter=..., usage_meter_margin=4
/ 3)` (or `ModelRouter(usage_meter=..., reserve_input_margin=4 / 3)`) scales the reserved input
estimate, here as if a token were three characters, rounding up. The default is 1.0 and a margin
below 1.0 is rejected. It changes only what the meter reserves: context packing, compaction and
the run's token budget keep the unscaled estimate, and `settle` still gets the reported usage.

`call` is a frozen `MeterCall` that says whose call it is and which model is being tried. One
router serves every tenant and run, so a multi-tenant meter charges from it rather than from
host context variables, which do not survive a pause or a restart. `settle` and `release` get
only the ticket, so keep the `MeterCall` in the ticket when you need it there.

| Field | Value |
| --- | --- |
| `tenant_id`, `user_id` | From the run request. |
| `run_id`, `session_id` | The run and its session. |
| `parent_run_id`, `conversation_id`, `turn_id` | From the run request, when set. |
| `agent`, `agent_version` | The agent's name and the version that runs. |
| `step_id` | The step that makes the call; `None` for compaction. |
| `purpose` | `"step"` for agent steps (retries and schema repairs included), `"compaction"` for history compaction, `"other"` for a call made on `ModelRouter` directly. |
| `attempt` | The retry attempt, from 0. A fallback to another model within one attempt keeps the attempt. |
| `provider` | The provider registration id (the `providers:` key) of the candidate being tried. |
| `model` | The model of the candidate being tried; a fallback reports its own. |
| `streaming` | Whether the call is streamed. |
| `metadata` | A read-only copy of `RunRequest.metadata`, for an account id such as a workspace. The runtime never puts these values in events, audits or spans. |

The run fields come from the persisted run, so a run resumed after an approval pause, or
recovered by `Runtime.recover()` after a restart, reports the same tenant, user and run as
before. A cached model response makes no call, so the meter never sees it. A call made on
`ModelRouter.generate` or `ModelRouter.stream` directly gets a `MeterCall` with only `provider`,
`model`, `streaming` and `purpose="other"`; pass `meter_call=` to supply the rest.

**The 0.1.0a14 form.** A meter whose `reserve` has no `call` parameter (and no `**kwargs`) still
works: the router inspects `reserve` once, when it is built, and then calls it without `call`. It
emits one `DeprecationWarning` per meter, attributed to the meter's module. That form is removed
in 0.2: add `call: MeterCall` to `reserve`; nothing else changes.

To refuse, raise `MeterRefusedError`. The runtime does not retry the call, does not try a
fallback model and does not count it against the provider's health; the run ends `failed` with
the message as its error, a `budget.refused` event naming the refused provider and model, and
a `budget.refused` attribute on the `agent.run` span (see [Operations](operations.md#usage-metering)). The meter holds its own
state, so a run paused for approval is charged again only for the calls it makes after it
resumes.

## Tool-call guards

Guards that keep a model from wasting a run: each refused or held-back call still answers the
model's tool call, with a note saying what to do instead, and never runs. Their state (refusal
counts, call counts, reads already made) is kept in `RunState`, so it survives an approval pause
and a restart. They were the Algen Agent Harness's `prechecks.py`, `repeats.py` and
`allowance.py`.

```yaml
agents:
  - name: people-os.assistant
    # ...
    max_steps: 30
    enabled_tools: [hr.find_people, hr.update_record, jobs.status]
    tool_policy:
      on_error: return_to_model
      max_refusals_per_tool: 3      # from the third refusal in a row: stop and tell the user
      repeat_guard: true            # an identical read with nothing changed returns a note
      max_calls_per_tool:
        hr.find_people: 5           # a search the model could keep rephrasing
    wrap_up_policy:
      enabled: true
      tool_calls_left: 2            # warn while two or fewer tool calls are affordable
      token_wrap_up_share: 0.75     # from 75% of the token budget: at most two more calls
      token_answer_share: 0.9       # from 90%: answer now
http_tools:
  - name: jobs.status
    # ...
    volatile: true                  # the status changes on its own: never a repeat
```

**Checks before a side effect.** Before a call runs, and before any approval is created, its
arguments are checked against the tool's `input_schema`; this is on for every tool. A tool can
also carry a `precheck`, which the Runtime calls for every call that is not read-only (side effect
other than `none` or `read`), whether or not the call needs approval:

```python
async def config_precheck(
    definition: ToolDefinition, arguments: Mapping[str, Any], earlier: Sequence[EarlierCall]
) -> str | None:
    """Why the change would be refused, or None. ``earlier`` holds the calls planned before
    this one in the same model reply, as (definition, arguments): judge the call after them."""
    ...

Tool(definition, handler, precheck=config_precheck)
# or: @tool(write(approval=True), precheck=config_precheck)
```

`earlier` is filled only when the calls of a reply run together (`parallel_calls`); otherwise the
earlier calls of the reply have already run, one at a time, and the precheck sees their effect.
A refused call is answered with `{"ok": false, "error": <problem>, "note": <note>}`: nothing ran
and nobody was asked. A call already approved is not prechecked again.

**Refusal limits.** Refusals (schema and precheck) are counted per tool. The note says
"Fix it and try again" until `max_refusals_per_tool`; from then on it says the tool has refused
the model that many times, not to call it again, and to tell the user what was refused and why.
With `refusal_count: consecutive` (the default) a call to the tool that runs successfully starts
its count again: the limit is for a model that cannot get a call right, not one that fixes its
mistakes along the way, so refused, succeeded, refused, refused is two refusals, not three. A
call that fails or is held back by another guard does not reset the count, and neither does a
successful call to another tool. `refusal_count: per_run` counts every
refusal in the run, as 0.1.0a17 did. The count is part of `RunState`, so either way it survives
an approval pause and a restart.

**Repeat guard and call limits.** With `repeat_guard`, a read-only call identical (tool and
arguments) to one that already succeeded in the run, with no successful state-changing call since,
returns `{"ok": false, "repeated": true, "error": "You already made this exact call ..."}`. A
successful write, external or destructive call lets every read run again; a failed read is
forgotten, so its retry runs. A tool marked `volatile` (`ToolDefinition.volatile`, `@tool(...,
volatile=True)`, `http_tools[].volatile`, and the live `starter.clock`) is never held back.
`max_calls_per_tool` caps how often a tool runs; past it the call returns "<tool> has run N
times in this turn. Answer from what it returned ...". A refused call does not count against the
limit. This guard is separate from the run-scoped tool cache: the cache hides a repeat from the
model, the guard tells the model it is repeating.

**Room to answer.** An answer takes two steps (the model call and the completion), and each more
tool call two (the call and the model call that reads it). With `wrap_up_policy.enabled`, after
each tool call the Runtime computes the tool calls still affordable from the step limit; from
`token_wrap_up_share` of the token budget that is capped at `tool_calls_left`, and from
`token_answer_share` it is 0. At or below `tool_calls_left`, the tool message carries the
wrap-up notice as a second text block beside the result, whatever the result's type. There is no
notice while `continuation_policy` still has automatic extensions left, since the limit is not
final yet.

Events, audit records and span attributes for these guards are listed in
[Operations](operations.md#tool-call-guards).

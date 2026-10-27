# Agent tool behaviour

Settings on `AgentDefinition` that change how an agent uses tools. Each is opt-in: an agent
that sets none of them behaves exactly as before. Together they let coding and assistant
agents recover from tool errors, keep going when the user declines an action, edit files that
contain secret-like text, run independent tool calls together, and remember what they read.

| Setting | Default | Opt-in behaviour |
| --- | --- | --- |
| `tool_policy.on_error` | `fail_run` | `return_to_model`: a failed tool call (tool not enabled or unknown, invalid arguments, a guardrail denial, an exception in the tool) becomes the call's result, `{"error": {"type", "message"}}`, and the run continues. Messages are the runtime's safe messages; a tool's own exception text is never shown. |
| `approval_policy.on_rejection` | `fail_run` | `continue`: rejecting an approval answers the tool call with `{"declined": true, "message", "reason"?}` and the run continues. Pass `"reason"` beside `"decision": "rejected"` in `resume()`. |
| `guardrail_policy.tool_policies` | `None` (use `policies`) | The policies applied to tool arguments and results. `()` applies none at tool boundaries, so a file is written exactly as the model wrote it. Messages sent to the model are still screened by `policies`. |
| `guardrail_policy.tool_result_injection` | `off` | `flag`: a tool result that tries to give the model instructions is wrapped as untrusted data with a warning before the model reads it. `block`: the call fails (combine with `on_error="return_to_model"` to tell the model). |
| `tool_policy.parallel_calls` | `false` | The tool calls one model reply asks for run concurrently, up to `max_parallel_calls` (default 4), and are recorded in the order asked. A reply that includes a call needing approval, or a memory, skill or working-state tool, runs one call at a time. |
| `memory_policy.store_tool_payloads` | `false` | Session memory keeps the turn's tool calls and results as well as the question and answer, each result capped at `tool_payload_max_chars` (default 4,000). History read back always starts at a whole turn, and a tool call is never kept without its result. |

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

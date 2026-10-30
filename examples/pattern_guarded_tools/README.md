# Guarded tools pattern

Typed shop tools behind the controls the Runtime applies around tool calls and model calls. The
model is the mock provider, scripted to make the mistakes each control exists for, so the run is
offline and deterministic:

```bash
python -m examples.pattern_guarded_tools.app            # what each control did
python -m examples.pattern_guarded_tools.app --verbose  # and every run event of the guard run
```

```text
tools.py    four typed tools, a precheck, a failure-as-data result and a logging wrapper
plugin.py   ShopPlugin, which registers the tools
agent.yaml  the plugin, tool_policy, wrap_up_policy, an output budget and a tenant-scoped tool-result cache
app.py      a UsageMeter, the scripted runs and the printout
```

| Control | Where | What the run shows |
| --- | --- | --- |
| Typed tools | `@tool(read(), name=...)` and `build_tool(function, write(), ...)` | Schemas from type hints, permissions and idempotency from the preset |
| Precheck | `build_tool(..., precheck=discount_precheck)` | An 80% discount is `refused (tool.precheck_refused)` before it runs or anyone is asked |
| Failure as data | `failure_field="ok"` | A discount on an unknown SKU returns `{"ok": false, "error": ...}`: recorded `failed`, the model still reads it |
| Refusal limit | `tool_policy.max_refusals_per_tool: 2`, `refusal_count: consecutive` | Refused, succeeded, failed, refused, refused: the success restarts the count (the failure does not), so only the last refusal is the second in a row and tells the model to stop |
| Repeat guard | `tool_policy.repeat_guard: true` | The same `shop.stock` read with nothing changed since is `repeated` instead of running |
| Call limit | `tool_policy.max_calls_per_tool: {shop.search: 2}` | The third search is `over_limit (tool.call_limit)` |
| Wrap-up notice | `wrap_up_policy: {enabled: true, tool_calls_left: 2}` with `max_steps: 26` | Once two tool calls are left, results carry a notice to stop calling tools and answer |
| Wrapped handler | `wrap(set_discount_tool, logged)` (`Tool.with_handler`) | The wrapper sees every call that reaches the handler; the precheck, `failure_field` and definition are kept |
| Cache keyed on the user | `cache_context={"user_id"}` with `cache.policies.tool_results` at tenant scope | alice, bob, alice: bob's call runs the handler, alice's second is served from the cache |
| Usage meter | `build_container(..., usage_meter=CreditMeter(...), usage_meter_margin=4 / 3)` | Every model call is reserved with its `MeterCall` (user, run, purpose, provider, model) and settled; carol, with no allowance, gets `MeterRefusedError` and her run fails |

The guards answer a call without running it, so their state (refusal and call counts, reads already
made) is kept in the run's state and survives an approval pause or a restart. The events, audit
records and span attributes each control leaves are listed in
[Operations: Tool-call guards](../../docs/operations.md#tool-call-guards) and
[Usage metering](../../docs/operations.md#usage-metering). The authoring side is in
[Writing agent tools](../../docs/tool-authoring.md) and
[Agent tool behaviour](../../docs/agent-tooling.md).

The tools are registered by `ShopPlugin`, declared in `agent.yaml`'s `domain_plugins`, so
`algen-agent-runtime describe` and Studio see them. The usage meter is a deployment setting passed
to `build_container`, not an `agent.yaml` field. Check the project with
`algen-agent-runtime check examples/pattern_guarded_tools/agent.yaml --path . --project-root examples/pattern_guarded_tools`.

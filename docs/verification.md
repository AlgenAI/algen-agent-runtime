# Answer verification

Before a run completes, its final answer goes through the verifiers the agent names. A failed
check sends the model a repair turn that says what failed, up to `max_repairs` times; after that
the run fails with `VerificationError`. Every check is reported on the `verification.completed`
event (`passed`, `confidence`, `reason_code`).

A run that failed verification delivers no answer: `RunResult.output` (and the run's
`output_text`, which `GET /v1/runs/{id}` shows) is empty, as on every run that did not complete.
The last rejected answer is kept for diagnostics in `RunResult.rejected_output` (an attribute
left out of `model_dump()` and JSON, so a result passed on whole, such as a sub-agent's tool
result, never carries it) and in the run view's `rejected_output`. Do not show it to users as
the answer.

### Structured output and guardrails

With a `response_schema`, the guardrails at `after_model` and `final_response` see the parsed
JSON answer (a `dict` or `list`, with `structured_output: True` in the policy context) instead of
its text. The built-in redactions then change string values only: keys, numbers and booleans keep
their types, and the answer stays valid JSON. An answer no guardrail changed is delivered exactly
as the model wrote it.

`after_model` runs before verification, so the verifiers check the redacted answer. When a
`final_response` guardrail changes an answer that already passed verification, the changed answer
is verified again (a second `verification.completed` event, with `stage: "final_response"`). If it
no longer passes, the run fails with `VerificationError` instead of delivering output that breaks
its schema; there is no repair turn, since the model's answer was valid. Plain-text answers are
unchanged: `final_response` guardrails rewrite their text after verification.

```yaml
verification_policy:
  verifiers: [non_empty, result_coverage] # default: [non_empty]
  minimum_confidence: 0.0
  max_repairs: 1
```

| Name | Checks | Model call |
| --- | --- | --- |
| `non_empty` | The answer is not blank. | No |
| `json_schema` | The answer matches the request's `response_schema`; added automatically when one is set. | No |
| `citations` | Cited `[S1]` identifiers exist and at least one is cited when evidence was retrieved; added with `guardrail_policy.require_citations`. | No |
| `result_coverage` | An answer that says something does not exist rests on a complete search ([below](#absence-claims-and-result-coverage)). | No |

Applications register their own verifiers (an object with a `name` and an async
`verify(value, context) -> VerificationResult`) with `runtime.verifiers.register(...)` or the
`verifiers` plugin registry, and name them in `verification_policy.verifiers`. A
`VerificationResult` carries `findings` (paths, names and counts, never values from the answer)
and a `repair_hint`; both go into the repair turn. `AnalyticalVerifier` checks analytical claims
against result cells and is called by analytical code, not named here.

## Absence claims and result coverage

An agent's read tools often query a derived store (a search index, a cache, a replica, an
extract), or return the first page of a long list. An empty or short result then reads to the
model as a fact: "this email has no attachment", "no application on record". The
`result_coverage` verifier catches that without a model call.

It reads the [`coverage`](tool-authoring.md#saying-what-a-result-covered) that tool results report
and fails the answer when all of these hold:

1. The answer states absence: it matches an absence pattern (`no`, `none`, `nothing`,
   `not found`, `does not exist`, `could not find` and similar).
2. A tool result in the current run (after the user's request, including results fetched after
   a repair turn) reported coverage that was not complete: it was `capped`, returned fewer items
   than its `total`, or read a `derived` source.
3. No result in the run searched the same collection (`searched`) completely.
4. The answer does not already say the search may be incomplete (`may be incomplete`,
   `can't confirm`, `limited to`, `only the first`, `capped` and similar).

The repair turn names each partial search (tool, collection, `derived` or `capped at 50 of 1200
results`; never filter values) and asks the model to search the system of record or say plainly
that the search may be incomplete. Results that report no coverage are ignored, so with tools that
do not report it the verifier always passes, and it is off until an agent names it.

| `reason_code` | Passed | Meaning |
| --- | --- | --- |
| `coverage.not_reported` | yes | No tool result in the run reported coverage. |
| `coverage.no_absence_claim` | yes | The answer does not state absence. |
| `coverage.complete` | yes | Every collection searched was searched completely at least once. |
| `coverage.disclosed` | yes | The answer says the search may be incomplete. |
| `coverage.partial_absence` | no | The answer states absence on a partial search. |

The check is a deliberately cautious heuristic. It cannot tell which result an absence claim
rests on, so a run that searched one collection partially and states absence about something
else asks for one repair it did not need; that costs one model turn and only on runs whose tools
report partial coverage. The patterns are English. Register your own instance to change them:

```python
from algen_agent_runtime.verification.coverage import (
    DEFAULT_ABSENCE_PATTERNS,
    ResultCoverageVerifier,
)

runtime.verifiers.register(
    ResultCoverageVerifier(
        absence_patterns=(*DEFAULT_ABSENCE_PATTERNS, r"\bkein(?:e|en)?\b"),
    )
)
```

Coverage survives [tool-result offloading](artifacts.md): the offloaded reference keeps the
result's `coverage`, so the model and the verifier still see it.

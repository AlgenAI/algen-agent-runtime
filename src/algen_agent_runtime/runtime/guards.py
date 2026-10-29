"""Tool-call guards: what the model is told when a call is refused, held back or nearly too late.

Ported from the Algen Agent Harness (``prechecks.py``, ``repeats.py``, ``allowance.py``) with
their wording. The Runtime applies them in ``AgentRuntime`` and keeps their state in
``RunState``, so it survives an approval pause and a restart.

- A call that would be refused (arguments that do not fit the tool's input schema, or a tool's
  precheck) never runs and is never put to the user; the model gets the problem back to fix.
  A model that cannot get a call right keeps trying and pays for the whole conversation each
  time, so from ``tool_policy.max_refusals_per_tool`` refusals it is told to stop and answer.
  By default the count is of refusals in a row: a successful call to the tool starts it again,
  since a model making progress is not stuck (``tool_policy.refusal_count="per_run"`` counts
  every refusal in the run).
- A read the model already made, with nothing changed since, returns a note instead of
  running again; ``tool_policy.max_calls_per_tool`` caps searches a model can keep rephrasing.
- Near the end of the run's steps or tokens, tool results carry a notice to wrap up and answer
  (``wrap_up_policy``), because a run that runs out ends without an answer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import jsonschema

from algen_agent_runtime.tools.contracts import SideEffect, ToolDefinition

FIX_IT = "Nothing was changed and the user was not asked. Fix it and try again."
STOP = (
    "Nothing was changed and the user was not asked. This tool has refused you {count} times "
    "in this turn: do not call it again. Answer the user now: say what you tried, what was "
    "refused and why, and what they can do."
)
REPEATED = (
    "You already made this exact call in this turn and nothing has changed since, so it would "
    "return the same result as before. Use that result, try something different, or answer "
    "the user with what you know."
)
# Steps a final answer needs: the model call that writes it and the run's completion.
ANSWER_STEPS = 2


def schema_problem(definition: ToolDefinition, arguments: Mapping[str, Any]) -> str | None:
    """Why the arguments do not fit the tool's input schema, or None."""
    validator = jsonschema.validators.validator_for(definition.input_schema)(
        definition.input_schema
    )
    error = jsonschema.exceptions.best_match(validator.iter_errors(dict(arguments)))
    if error is None:
        return None
    where = ".".join(map(str, error.absolute_path))
    return (
        f"Invalid arguments for {definition.name}: {f'{where}: ' if where else ''}{error.message}"
    )


def refusal_note(count: int, limit: int | None) -> str:
    """What the model is told with a refusal: fix it, or after too many, stop."""
    return STOP.format(count=count) if limit is not None and count >= limit else FIX_IT


def over_limit(name: str, limit: int) -> str:
    return (
        f"{name} has run {limit} times in this turn. Answer from what it returned, or say what "
        "you could not find; do not search again."
    )


def read_only(definition: ToolDefinition) -> bool:
    return definition.side_effect in {SideEffect.NONE, SideEffect.READ}


def call_key(name: str, arguments: Mapping[str, Any]) -> str:
    """A digest identifying a call by tool and arguments, kept in the checkpoint."""
    material = name + ":" + json.dumps(arguments, sort_keys=True, default=str)
    return hashlib.sha256(material.encode()).hexdigest()


def tool_calls_left(max_steps: int, used: int) -> int:
    """Tool calls, one per reply, the run can still make and then answer.

    Each tool call is a step, and so is the model call that reads its result.
    """
    return max(0, (max_steps - used - ANSWER_STEPS) // 2)


def wrap_up_note(calls: int) -> str:
    if calls == 0:
        return (
            "This turn is out of steps or tokens. Do not call any tool; write your final answer "
            "now: what you did, what is unfinished, and what the user should do next."
        )
    return (
        f"This turn is nearly out of steps or tokens: at most {calls} more tool calls, then you "
        "must answer. Stop calling tools unless one is essential, and write your final answer: "
        "what you did, what is unfinished, and what the user should do next."
    )

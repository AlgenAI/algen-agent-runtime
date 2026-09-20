from __future__ import annotations

from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.types.contracts import RunStatus

ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.RECEIVED: frozenset({RunStatus.VALIDATING, RunStatus.CANCELLED}),
    RunStatus.VALIDATING: frozenset(
        {RunStatus.BUILDING_CONTEXT, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.BUILDING_CONTEXT: frozenset(
        {RunStatus.PLANNING, RunStatus.RETRYING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.PLANNING: frozenset(
        {
            RunStatus.INVOKING_MODEL,
            RunStatus.INVOKING_TOOL,
            RunStatus.AWAITING_CLARIFICATION,
            RunStatus.AWAITING_APPROVAL,
            RunStatus.VERIFYING,
            RunStatus.COMPOSING,
            RunStatus.RETRYING,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.AWAITING_CLARIFICATION: frozenset(
        {RunStatus.BUILDING_CONTEXT, RunStatus.CANCELLED, RunStatus.TIMED_OUT}
    ),
    RunStatus.AWAITING_APPROVAL: frozenset(
        {RunStatus.PLANNING, RunStatus.CANCELLED, RunStatus.FAILED, RunStatus.TIMED_OUT}
    ),
    RunStatus.INVOKING_MODEL: frozenset(
        {
            RunStatus.PLANNING,
            RunStatus.VERIFYING,
            RunStatus.RETRYING,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
        }
    ),
    RunStatus.INVOKING_TOOL: frozenset(
        {
            RunStatus.BUILDING_CONTEXT,
            RunStatus.PLANNING,
            RunStatus.RETRYING,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
        }
    ),
    RunStatus.VERIFYING: frozenset(
        {RunStatus.COMPOSING, RunStatus.RETRYING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.RETRYING: frozenset(
        {
            RunStatus.BUILDING_CONTEXT,
            RunStatus.PLANNING,
            RunStatus.INVOKING_MODEL,
            RunStatus.INVOKING_TOOL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
        }
    ),
    RunStatus.COMPOSING: frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.TIMED_OUT: frozenset(),
}


def validate_transition(current: RunStatus, target: RunStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ConflictError(f"invalid run transition: {current.value} -> {target.value}")

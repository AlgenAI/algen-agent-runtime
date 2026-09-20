import pytest

from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.runtime.state_machine import validate_transition
from algen_agent_runtime.types.contracts import RunStatus


def test_legal_transition() -> None:
    validate_transition(RunStatus.RECEIVED, RunStatus.VALIDATING)


def test_illegal_transition_is_rejected() -> None:
    with pytest.raises(ConflictError):
        validate_transition(RunStatus.RECEIVED, RunStatus.COMPLETED)

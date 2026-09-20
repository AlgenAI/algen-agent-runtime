import pytest
from pydantic import ValidationError

from algen_agent_runtime.types.contracts import (
    AgentDefinition,
    ImageBlock,
    Message,
    ModelProfile,
    Role,
)


def test_message_helper_and_immutable_contract() -> None:
    message = Message.text(Role.USER, "hello")
    assert message.text_content == "hello"
    with pytest.raises(ValidationError):
        message.role = Role.ASSISTANT


def test_agent_version_and_step_bounds_are_validated() -> None:
    with pytest.raises(ValidationError):
        AgentDefinition(
            name="Bad Name",
            version="latest",
            description="bad",
            system_instructions="x",
            default_model=ModelProfile(name="x"),
            max_steps=0,
        )


def test_image_requires_exactly_one_source() -> None:
    with pytest.raises(ValidationError):
        ImageBlock()
    with pytest.raises(ValidationError):
        ImageBlock(url="https://example.com/a.png", data="abc")

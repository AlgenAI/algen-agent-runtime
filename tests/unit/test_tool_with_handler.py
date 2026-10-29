"""``Tool.with_handler`` and ``wrap`` decorate a handler without dropping any hook."""

from __future__ import annotations

import inspect
from typing import Any

from algen_agent_runtime.tools.contracts import (
    ReconcileOutcome,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolHandler,
    wrap,
)
from algen_agent_runtime.tools.testing import fake_tool_context


def _definition() -> ToolDefinition:
    return ToolDefinition(
        name="config.change",
        version="1",
        description="Change a setting.",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        side_effect=SideEffect.WRITE,
    )


def _handler(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return {"ran": "original"}


def _replacement(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return {"ran": "replacement"}


def _fully_hooked() -> Tool:
    """A tool with every ``Tool.__init__`` parameter set to a value that is not its default."""

    def compensation(result: Any, context: ToolContext) -> None:
        return None

    def replay_validator(result: Any, context: ToolContext) -> None:
        return None

    def precheck(definition: Any, arguments: Any, earlier: Any) -> str | None:
        return "refused"

    def reconcile(record: Any, context: ToolContext) -> ReconcileOutcome:
        return ReconcileOutcome(status="unknown")

    tool = Tool(
        _definition(),
        _handler,
        compensation,
        replay_validator=replay_validator,
        precheck=precheck,
        reconcile=reconcile,
        reconcile_on_replay=False,
    )
    tool.__doc__ = "Change a setting."
    # Every constructor parameter is set away from its default, so a hook added later and
    # forgotten here fails this check instead of passing by default.
    for name, parameter in inspect.signature(Tool.__init__).parameters.items():
        if name in {"self", "definition", "handler"}:
            continue
        assert getattr(tool, name) is not parameter.default, name
    return tool


def test_with_handler_keeps_every_other_attribute() -> None:
    original = _fully_hooked()
    replaced = original.with_handler(_replacement)

    assert type(replaced) is Tool and replaced is not original
    assert replaced.handler is _replacement
    kept = {name: value for name, value in vars(original).items() if name != "handler"}
    assert kept, "the tool has attributes to keep"
    for name, value in kept.items():
        assert getattr(replaced, name) is value, name
    assert set(vars(replaced)) == set(vars(original))
    # The original is unchanged.
    assert original.handler is _handler


async def test_with_handler_runs_the_new_handler_and_keeps_the_precheck() -> None:
    replaced = _fully_hooked().with_handler(_replacement)
    assert await replaced.execute({}, fake_tool_context(replaced)) == {"ran": "replacement"}
    assert await replaced.check({}) == "refused"


def test_with_handler_keeps_a_subclass_and_its_own_attributes() -> None:
    class AuditedTool(Tool):
        owner = "platform"

    original = AuditedTool(_definition(), _handler)
    original.extra = {"note": "kept"}  # type: ignore[attr-defined]
    replaced = original.with_handler(_replacement)
    assert isinstance(replaced, AuditedTool)
    assert replaced.extra is original.extra  # type: ignore[attr-defined]


async def test_wrap_decorates_the_handler_and_keeps_every_hook() -> None:
    seen: list[dict[str, Any]] = []

    def logged(handler: ToolHandler) -> ToolHandler:
        async def run(arguments: dict[str, Any], context: ToolContext) -> Any:
            seen.append(arguments)
            result = handler(arguments, context)
            return await result if inspect.isawaitable(result) else result

        return run

    original = _fully_hooked()
    wrapped = wrap(original, logged)
    assert await wrapped.execute({"key": "a"}, fake_tool_context(wrapped)) == {"ran": "original"}
    assert seen == [{"key": "a"}]
    for name, value in vars(original).items():
        if name != "handler":
            assert getattr(wrapped, name) is value, name

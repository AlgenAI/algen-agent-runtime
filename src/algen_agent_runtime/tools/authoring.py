"""Write a tool as a typed function: ``@tool``, policy presets and ``Toolkit``.

The decorator returns an ordinary :class:`Tool`, so the executor, policies and ledger treat it
like any other tool.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import typing
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol, overload

from pydantic import BaseModel, ConfigDict, TypeAdapter, create_model

from algen_agent_runtime.config.settings import EnvironmentSecretProvider
from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)
from algen_agent_runtime.types.contracts import RetryPolicy

DEFAULT_TIMEOUT_SECONDS = 30.0
LONG_RUNNING_TIMEOUT_SECONDS = 180.0

SecretResolver = Callable[[str], Awaitable[str]]


@dataclass(frozen=True)
class ApprovalRequest:
    """One call of a tool whose preset names a deployment approval handler."""

    definition: ToolDefinition
    arguments: dict[str, Any]
    context: ToolContext


class ApprovalHandler(Protocol):
    """A deployment's own approval flow around a tool call.

    ``handle`` decides what the call does: run ``execute`` (approved already, or approval not
    needed), record the request and return a result that says approval was requested, or raise.
    The Runtime defines only this protocol; the flow (a change request, a ticket, an email) belongs
    to the deployment.
    """

    async def handle(
        self, request: ApprovalRequest, execute: Callable[[], Awaitable[Any]]
    ) -> Any: ...


@dataclass(frozen=True)
class ToolPreset:
    """The governance fields a tool's side effect implies, set together.

    Override any field with :meth:`replace`, for example
    ``write(approval=True).replace(timeout_seconds=10)``.
    """

    side_effect: SideEffect
    access: typing.Literal["read", "write"]
    idempotency: Idempotency
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_attempts: int | None = None
    """``None`` keeps the Runtime's default retry policy; writes are single-attempt."""
    requires_approval: bool = False
    approval_handler: ApprovalHandler | None = None
    permissions: frozenset[str] | None = None
    """Explicit permissions; ``None`` means ``<permission prefix>.<access>``."""

    def replace(self, **changes: Any) -> ToolPreset:
        return dataclasses.replace(self, **changes)


def _approval(approval: bool | ApprovalHandler) -> dict[str, Any]:
    if isinstance(approval, bool):
        return {"requires_approval": approval}
    return {"approval_handler": approval}


def read(*, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> ToolPreset:
    """Reads state: ``<prefix>.read``, idempotent, retried on transient errors."""
    return ToolPreset(SideEffect.READ, "read", Idempotency.IDEMPOTENT, timeout)


def write(
    *, approval: bool | ApprovalHandler = False, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> ToolPreset:
    """Changes state: ``<prefix>.write``, keyed by the idempotency key, a single attempt.

    ``approval=True`` always asks for the Runtime's approval; an :class:`ApprovalHandler` runs the
    deployment's own flow instead. ``False`` leaves approval to the agent's approval policy.
    """
    return ToolPreset(
        SideEffect.WRITE, "write", Idempotency.KEYED, timeout, 1, **_approval(approval)
    )


def external(
    *, approval: bool | ApprovalHandler = False, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> ToolPreset:
    """Acts outside the system (sends a message, calls a partner): like :func:`write`."""
    return ToolPreset(
        SideEffect.EXTERNAL, "write", Idempotency.KEYED, timeout, 1, **_approval(approval)
    )


def destructive(
    *, approval: bool | ApprovalHandler = True, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> ToolPreset:
    """Deletes or cannot be undone: like :func:`write`, and approval is on by default."""
    return ToolPreset(
        SideEffect.DESTRUCTIVE, "write", Idempotency.KEYED, timeout, 1, **_approval(approval)
    )


def long_running(
    base: ToolPreset | None = None, *, timeout: float = LONG_RUNNING_TIMEOUT_SECONDS
) -> ToolPreset:
    """A slow tool (``base`` defaults to :func:`read`): a longer timeout and a single attempt,
    so a timeout never starts a second run."""
    return (base or read()).replace(timeout_seconds=timeout, max_attempts=1)


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    definitions = schema.get("$defs", {})

    def resolve(node: Any, seen: tuple[str, ...]) -> Any:
        if isinstance(node, dict):
            reference = node.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                name = reference.removeprefix("#/$defs/")
                if name in seen:
                    raise ConfigurationError(f"recursive model {name!r} cannot be a tool schema")
                merged = {**definitions[name], **{k: v for k, v in node.items() if k != "$ref"}}
                return resolve(merged, (*seen, name))
            return {key: resolve(value, seen) for key, value in node.items() if key != "$defs"}
        if isinstance(node, list):
            return [resolve(item, seen) for item in node]
        return node

    resolved: dict[str, Any] = resolve(schema, ())
    return resolved


def _strict(node: Any) -> Any:
    """Drop titles and ``additionalProperties: true`` (the default), and close every object
    that lists its properties."""
    if isinstance(node, list):
        return [_strict(item) for item in node]
    if not isinstance(node, dict):
        return node
    result: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title" or (key == "additionalProperties" and value is True):
            continue
        if key == "properties" and isinstance(value, dict):
            result[key] = {name: _strict(schema) for name, schema in value.items()}
        else:
            result[key] = _strict(value)
    if result.get("type") == "object" and "properties" in result:
        result.setdefault("additionalProperties", False)
    return result


def json_schema(annotation: Any) -> dict[str, Any]:
    """A strict, self-contained JSON Schema for a type hint or Pydantic model."""
    schema = TypeAdapter(annotation).json_schema()
    strict: dict[str, Any] = _strict(_inline_refs(schema))
    return strict


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


@dataclass(frozen=True)
class _Signature:
    input_model: type[BaseModel]
    whole_model: str | None
    fields: tuple[str, ...]
    context_parameter: str | None
    output: Any


def _signature(function: Callable[..., Any]) -> _Signature:
    try:
        hints = typing.get_type_hints(function, include_extras=True)
    except NameError as exc:
        raise ConfigurationError(
            f"tool {function.__name__!r}: cannot resolve a type hint ({exc}); define the types "
            "it names at module level"
        ) from exc
    parameters = inspect.signature(function).parameters
    context_parameter = None
    fields: dict[str, Any] = {}
    for name, parameter in parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            raise ConfigurationError(
                f"tool {function.__name__!r}: *args and **kwargs are not allowed"
            )
        if name not in hints:
            raise ConfigurationError(
                f"tool {function.__name__!r}: parameter {name!r} needs a type hint"
            )
        if hints[name] is ToolContext:
            context_parameter = name
            continue
        default = ... if parameter.default is parameter.empty else parameter.default
        fields[name] = (hints[name], default)
    output = hints.get("return", Any)
    if len(fields) == 1:
        [(name, (annotation, default))] = fields.items()
        if _is_model(annotation) and default is ...:
            return _Signature(annotation, name, (name,), context_parameter, output)
    model = create_model(
        f"{function.__name__}_arguments",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    return _Signature(model, None, tuple(fields), context_parameter, output)


def _description(function: Callable[..., Any], description: str | None) -> str:
    text = description or inspect.getdoc(function)
    if not text:
        raise ConfigurationError(
            f"tool {function.__name__!r} needs a docstring or description; the model reads it"
        )
    return text.strip()


def build_tool(
    function: Callable[..., Any],
    preset: ToolPreset | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    version: str = "1.0.0",
    permission_prefix: str | None = None,
    max_concurrency: int = 10,
    max_result_bytes: int = 1_048_576,
    cost_usd: float = 0,
    audit_metadata: dict[str, str] | None = None,
) -> Tool:
    """The :class:`Tool` for a typed function; ``@tool`` and ``Toolkit.tool`` call this."""
    preset = preset or read()
    tool_name = name or function.__name__
    signature = _signature(function)
    prefix = permission_prefix or tool_name.split(".", 1)[0]
    metadata = dict(audit_metadata or {})
    if preset.approval_handler is not None:
        metadata.setdefault("approval", "deployment")
    output_adapter: TypeAdapter[Any] = TypeAdapter(signature.output)
    definition = ToolDefinition(
        name=tool_name,
        version=version,
        description=_description(function, description),
        input_schema=json_schema(signature.input_model),
        output_schema={} if signature.output is Any else json_schema(signature.output),
        required_permissions=(
            preset.permissions
            if preset.permissions is not None
            else frozenset({f"{prefix}.{preset.access}"})
        ),
        side_effect=preset.side_effect,
        idempotency=preset.idempotency,
        timeout_seconds=preset.timeout_seconds,
        retry_policy=(
            RetryPolicy(max_attempts=preset.max_attempts)
            if preset.max_attempts is not None
            else RetryPolicy()
        ),
        cost_usd=cost_usd,
        max_concurrency=max_concurrency,
        max_result_bytes=max_result_bytes,
        audit_metadata=metadata,
        requires_approval=preset.requires_approval,
    )

    async def run(arguments: dict[str, Any], context: ToolContext) -> Any:
        validated = signature.input_model.model_validate(arguments)
        if signature.whole_model is not None:
            keyword_arguments: dict[str, Any] = {signature.whole_model: validated}
        else:
            keyword_arguments = {field: getattr(validated, field) for field in signature.fields}
        if signature.context_parameter is not None:
            keyword_arguments[signature.context_parameter] = context
        result = function(**keyword_arguments)
        if inspect.isawaitable(result):
            result = await result
        return output_adapter.dump_python(result, mode="json")

    approval = preset.approval_handler

    async def handler(arguments: dict[str, Any], context: ToolContext) -> Any:
        if approval is None:
            return await run(arguments, context)
        return await approval.handle(
            ApprovalRequest(definition, arguments, context), lambda: run(arguments, context)
        )

    tool = Tool(definition, handler)
    tool.__doc__ = function.__doc__
    return tool


@overload
def tool(function: Callable[..., Any], /) -> Tool: ...


@overload
def tool(
    preset: ToolPreset | None = None, /, **options: Any
) -> Callable[[Callable[..., Any]], Tool]: ...


def tool(
    preset: ToolPreset | Callable[..., Any] | None = None, /, **options: Any
) -> Tool | Callable[[Callable[..., Any]], Tool]:
    """Turn a typed function into a :class:`Tool`.

    ``@tool``, ``@tool(write(approval=True))`` or ``@tool(read(), name="crm.find_account")``.
    The input schema comes from the parameters' type hints (a single Pydantic-model parameter is
    the whole input), the output schema from the return hint and the description from the
    docstring. A parameter annotated ``ToolContext`` receives the call's context. The default
    preset is :func:`read`.
    """
    if callable(preset) and not isinstance(preset, ToolPreset):
        return build_tool(preset, **options)

    def decorate(function: Callable[..., Any]) -> Tool:
        return build_tool(function, preset, **options)

    return decorate


ClientFactory = Callable[[SecretResolver], Any]


class Toolkit:
    """Tools that share a client, configuration, ``env://`` secrets and a permission prefix.

    A plugin registers it with ``toolkit.register(context)``: the tools join the container, the
    toolkit becomes a container resource (its client opens at start or on first use and closes
    with the container) and its ``env`` names reach ``describe``.
    """

    def __init__(
        self,
        name: str,
        *,
        permission_prefix: str | None = None,
        env: Iterable[str] = (),
        client: ClientFactory | None = None,
    ) -> None:
        self.name = name
        self.permission_prefix = permission_prefix or name
        self.env = tuple(env)
        self._client_factory = client
        self._client: Any = None
        self._client_lock = asyncio.Lock()
        self._secrets: SecretResolver = EnvironmentSecretProvider().get
        self._tools: list[Tool] = []

    @property
    def tools(self) -> tuple[Tool, ...]:
        return tuple(self._tools)

    def tool(
        self, preset: ToolPreset | None = None, /, **options: Any
    ) -> Callable[[Callable[..., Any]], Tool]:
        """Like :func:`tool`, named ``<toolkit>.<function>`` under the toolkit's prefix."""

        def decorate(function: Callable[..., Any]) -> Tool:
            options.setdefault("name", f"{self.name}.{function.__name__}")
            options.setdefault("permission_prefix", self.permission_prefix)
            built = build_tool(function, preset, **options)
            self._tools.append(built)
            return built

        return decorate

    async def client(self) -> Any:
        """The shared client, created on first use from the toolkit's factory."""
        if self._client_factory is None:
            raise ConfigurationError(f"toolkit {self.name!r} has no client factory")
        async with self._client_lock:
            if self._client is None:
                created = self._client_factory(self._secrets)
                self._client = await created if inspect.isawaitable(created) else created
        return self._client

    def register(self, context: Any) -> None:
        """Add the tools to a plugin context; called from a plugin's ``register``."""
        secrets = getattr(context, "secrets", None)
        if secrets is not None:
            self._secrets = secrets
        if self.env and hasattr(context, "declare_env"):
            context.declare_env(*self.env)
        if self._client_factory is not None:
            context.add_resource(self)
        for item in self._tools:
            context.tools.register(item)

    async def start(self) -> None:
        if self._client_factory is not None:
            await self.client()

    async def aclose(self) -> None:
        async with self._client_lock:
            client, self._client = self._client, None
        if client is None:
            return
        close = getattr(client, "aclose", None) or getattr(client, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result

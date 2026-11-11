"""Hand Runtime tools to a foreign framework, so their calls stay governed (#119).

An adapter governs the outer call only: the model calls and tools a framework runs on its own are
the framework's. A tool handed over through :class:`GovernedToolBridge` is different. Each call
goes through the Runtime's ``ToolExecutor``, so permissions, input validation, policy, guards,
idempotency, the execution ledger and tool spans apply exactly as they do in a Runtime run.

Two things do not carry over:

- **Approvals.** A foreign framework cannot pause a Runtime run, so a tool that
  ``requires_approval`` is refused through the bridge. Keep such tools in a Runtime agent or
  workflow, or use the framework's own human-in-the-loop for them.
- **Model metering.** The bridge governs tool calls. The framework's model calls are not
  metered or routed by the Runtime.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from itertools import count
from typing import Any

from algen_agent_runtime.exceptions.errors import AlgenAgentRuntimeError
from algen_agent_runtime.frameworks.contracts import FrameworkRunRequest
from algen_agent_runtime.tools.contracts import ToolContext, ToolDefinition
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry

_FRAMEWORK_NAME = re.compile(r"[^A-Za-z0-9_-]")


def framework_tool_name(name: str) -> str:
    """The tool's name as frameworks accept it: ``crm.find_account`` becomes ``crm_find_account``."""
    return _FRAMEWORK_NAME.sub("_", name)


class GovernedToolBridge:
    """Runtime tools for one framework run, each call executed by the Runtime.

    ``permissions`` are what the caller may do in this run, as for a Runtime run; a tool whose
    ``required_permissions`` they lack is refused. Calls get the run's identity from ``request``
    and an idempotency key per call. In a host built with ``build_container``, pass
    ``container.runtime.tool_executor`` and ``container.tools``.
    """

    def __init__(
        self,
        executor: ToolExecutor,
        registry: ToolRegistry,
        request: FrameworkRunRequest,
        *,
        permissions: Iterable[str] = (),
        secrets: Mapping[str, str] | None = None,
    ) -> None:
        self._executor = executor
        self._registry = registry
        self._request = request
        self._permissions = frozenset(permissions)
        self._secrets = dict(secrets or {})
        self._calls = count(1)

    def definition(self, name: str) -> ToolDefinition:
        return self._registry.get(name).definition

    def _context(self, name: str) -> ToolContext:
        number = next(self._calls)
        return ToolContext(
            run_id=self._request.run_id,
            step_id=f"framework:{number}",
            tenant_id=self._request.tenant_id,
            user_id=self._request.user_id,
            session_id=self._request.session_id,
            permissions=self._permissions,
            idempotency_key=f"{self._request.run_id}:framework:{number}:{name}",
            secrets=self._secrets,
        )

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        """Run one call through the executor and return the tool's value.

        Raises what the executor raises: ``PolicyDeniedError`` for a refused call (including a
        tool that requires approval), ``ToolExecutionError`` for invalid input or a failed tool.
        """
        result = await self._executor.execute(name, arguments, self._context(name))
        return result.value

    async def call_for_model(self, name: str, arguments: dict[str, Any]) -> str:
        """Like :meth:`call`, but a refusal or failure becomes text the framework's model reads."""
        try:
            value = await self.call(name, arguments)
        except AlgenAgentRuntimeError as exc:
            return json.dumps({"error": type(exc).__name__, "message": str(exc)})
        return value if isinstance(value, str) else json.dumps(value, default=str)

    def openai_agents_tools(self, *names: str) -> list[Any]:
        """``FunctionTool`` objects for the OpenAI Agents SDK (the ``openai-agents`` extra)."""
        try:
            from agents import FunctionTool
        except ModuleNotFoundError as exc:
            from algen_agent_runtime.extras import MissingExtraError

            raise MissingExtraError(
                "GovernedToolBridge.openai_agents_tools", "openai-agents", missing=exc.name
            ) from exc

        tools: list[Any] = []
        for name in names:
            definition = self.definition(name)

            async def invoke(_context: Any, raw: str, *, _name: str = name) -> str:
                return await self.call_for_model(_name, json.loads(raw or "{}"))

            tools.append(
                FunctionTool(
                    name=framework_tool_name(name),
                    description=definition.description,
                    params_json_schema=dict(definition.input_schema),
                    on_invoke_tool=invoke,
                    strict_json_schema=False,
                )
            )
        return tools

    def langchain_tools(self, *names: str) -> list[Any]:
        """``StructuredTool`` objects for LangGraph and LangChain (the ``langgraph`` extra)."""
        try:
            from langchain_core.tools import StructuredTool
        except ModuleNotFoundError as exc:
            from algen_agent_runtime.extras import MissingExtraError

            raise MissingExtraError(
                "GovernedToolBridge.langchain_tools", "langgraph", missing=exc.name
            ) from exc

        tools: list[Any] = []
        for name in names:
            definition = self.definition(name)

            async def invoke(*, _name: str = name, **arguments: Any) -> str:
                return await self.call_for_model(_name, arguments)

            tools.append(
                StructuredTool.from_function(
                    coroutine=invoke,
                    name=framework_tool_name(name),
                    description=definition.description,
                    args_schema=dict(definition.input_schema),
                )
            )
        return tools

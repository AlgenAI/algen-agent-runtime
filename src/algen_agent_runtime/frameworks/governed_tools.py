"""Hand Runtime tools to a foreign framework, so their calls stay governed (#119).

An adapter governs the outer call only: the model calls and tools a framework runs on its own are
the framework's. A tool handed over through :class:`GovernedToolBridge` is different. Each call
goes through the Runtime's ``ToolExecutor``, so permissions, input validation, policy, guards,
idempotency, the execution ledger and tool spans apply exactly as they do in a Runtime run.

Two things do not carry over:

- **Approvals.** A foreign framework cannot pause a Runtime run, so a call that would need an
  approval in a Runtime run is refused through the bridge: a tool that ``requires_approval`` and,
  under the default :class:`ApprovalPolicy`, every tool with ``write``, ``external`` or
  ``destructive`` side effects. Keep such tools in a Runtime agent or workflow, use the
  framework's own human-in-the-loop for them, or pass an ``approval_policy`` that allows them.
- **Model metering.** The bridge governs tool calls. The framework's model calls are not
  metered or routed by the Runtime.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import uuid4

from algen_agent_runtime.exceptions.errors import (
    AlgenAgentRuntimeError,
    PolicyDeniedError,
    ToolExecutionError,
)
from algen_agent_runtime.frameworks.contracts import FrameworkRunRequest
from algen_agent_runtime.observability.safe_spans import runtime_tracer
from algen_agent_runtime.policies.contracts import PolicyAction
from algen_agent_runtime.policies.instrumentation import evaluate_policy_observed
from algen_agent_runtime.security.redaction import redact_error_text
from algen_agent_runtime.tools.contracts import SideEffect, ToolContext, ToolDefinition
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.types.contracts import ApprovalPolicy

_FRAMEWORK_NAME = re.compile(r"[^A-Za-z0-9_-]")
_STATE_CHANGING = frozenset({SideEffect.WRITE, SideEffect.EXTERNAL, SideEffect.DESTRUCTIVE})


def framework_tool_name(name: str) -> str:
    """The tool's name as frameworks accept it: ``crm.find_account`` becomes ``crm_find_account``."""
    return _FRAMEWORK_NAME.sub("_", name)


def _approval_reason(definition: ToolDefinition, policy: ApprovalPolicy) -> str | None:
    """Why a Runtime run would ask for an approval before this call, or None: the rule the
    mandatory ``side_effects`` policy applies at ``plan_tool``."""
    if definition.requires_approval:
        return "the tool requires_approval"
    side_effect = definition.side_effect
    if policy.require_for_destructive and side_effect == SideEffect.DESTRUCTIVE:
        return "it has destructive side effects"
    if policy.require_for_side_effects and side_effect in _STATE_CHANGING:
        return f"it has {side_effect.value} side effects"
    return None


class GovernedToolBridge:
    """Runtime tools for one framework run, each call executed by the Runtime.

    ``permissions`` are what the caller may do in this run, as for a Runtime run; a tool whose
    ``required_permissions`` they lack is refused. Calls get the run's identity from ``request``
    and a unique idempotency key per call. In a host built with ``build_container``, pass
    ``container.runtime.tool_executor``, ``container.tools`` and, to apply the deployment's
    ``plan_tool`` policies, ``policies=container.runtime.policies``.

    Each call is first checked as a Runtime run checks it at ``plan_tool``, since a framework
    cannot pause for a Runtime approval:

    - ``approval_policy`` (default ``ApprovalPolicy()``, which asks for an approval before any
      ``write``, ``external`` or ``destructive`` call) decides which calls would need an approval;
      the bridge refuses those with ``PolicyDeniedError``. Pass a policy that allows them, such as
      ``ApprovalPolicy(require_for_side_effects=False, require_for_destructive=False)``, only when
      something else gates them. A tool that ``requires_approval`` is always refused.
    - ``policies``, when given, is evaluated at ``plan_tool``: ``deny``, ``require_approval``,
      ``redact`` and ``require_clarification`` refuse the call, and ``transform`` rewrites its
      arguments (the executor then validates them against the input schema).
    """

    def __init__(
        self,
        executor: ToolExecutor,
        registry: ToolRegistry,
        request: FrameworkRunRequest,
        *,
        permissions: Iterable[str] = (),
        secrets: Mapping[str, str] | None = None,
        approval_policy: ApprovalPolicy | None = None,
        policies: Any = None,
    ) -> None:
        self._executor = executor
        self._registry = registry
        self._request = request
        self._permissions = frozenset(permissions)
        self._secrets = dict(secrets or {})
        self._approval_policy = approval_policy or ApprovalPolicy()
        self._policies = policies
        self._tracer = runtime_tracer("algen_agent_runtime.frameworks")

    def definition(self, name: str) -> ToolDefinition:
        return self._registry.get(name).definition

    def _context(self, name: str) -> ToolContext:
        # A random call id, not a per-bridge counter: a second bridge for the same run must never
        # be handed the recorded result of another call that happened to get the same number.
        call_id = uuid4().hex
        return ToolContext(
            run_id=self._request.run_id,
            step_id=f"framework:{call_id}",
            tenant_id=self._request.tenant_id,
            user_id=self._request.user_id,
            session_id=self._request.session_id,
            permissions=self._permissions,
            idempotency_key=f"{self._request.run_id}:framework:{call_id}:{name}",
            secrets=self._secrets,
        )

    async def _plan(
        self, name: str, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        """The arguments the call runs with after the ``plan_tool`` checks. Raises
        ``PolicyDeniedError`` for a call a Runtime run would refuse or pause for an approval."""
        definition = self.definition(name)
        reason = _approval_reason(definition, self._approval_policy)
        if reason is not None:
            raise PolicyDeniedError(
                f"tool {name} requires an approval for this call ({reason}), and a framework "
                "run cannot pause for one; run it from a Runtime agent or workflow, or pass "
                "GovernedToolBridge an approval_policy that allows it"
            )
        if self._policies is None:
            return arguments
        decision = await evaluate_policy_observed(
            tracer=self._tracer,
            engine=self._policies,
            point="plan_tool",
            payload=arguments,
            context={
                "tool": definition,
                "context": context,
                "approval_policy": self._approval_policy,
                "run_id": context.run_id,
                "tenant_id": context.tenant_id,
                "user_id": context.user_id,
            },
            attributes={
                "run.id": context.run_id,
                "tenant.id": context.tenant_id,
                "user.id": context.user_id,
                "tool.name": name,
            },
        )
        action = decision.action
        if action == PolicyAction.ALLOW:
            return arguments
        if action == PolicyAction.REQUIRE_APPROVAL:
            raise PolicyDeniedError(
                f"tool {name} requires an approval for this call ({decision.reason_code}), and "
                "a framework run cannot pause for one"
            )
        if action == PolicyAction.TRANSFORM:
            if not isinstance(decision.value, dict):
                raise ToolExecutionError(f"policy-transformed input for {name} is not an object")
            return decision.value
        # deny, redact (the tool is never sent masked arguments), require_clarification.
        raise PolicyDeniedError(f"{decision.reason_code}: {decision.reason}")

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        """Run one call through the ``plan_tool`` checks and the executor; return its value.

        Raises ``PolicyDeniedError`` for a refused call (including one that would need an
        approval), ``ToolExecutionError`` for invalid input or a failed tool.
        """
        context = self._context(name)
        arguments = await self._plan(name, arguments, context)
        result = await self._executor.execute(name, arguments, context)
        return result.value

    async def call_for_model(self, name: str, arguments: dict[str, Any]) -> str:
        """Like :meth:`call`, but a refusal or failure becomes text the framework's model reads."""
        try:
            value = await self.call(name, arguments)
        except AlgenAgentRuntimeError as exc:
            # Masked like a run's error: the framework's model, traces and logs read it (0.2.0b5).
            return json.dumps({"error": type(exc).__name__, "message": redact_error_text(exc)})
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

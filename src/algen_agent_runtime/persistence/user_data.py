"""Export or erase everything the Runtime stores about one user of a tenant.

``UserDataService`` works across the stores a deployment configures (in memory, Redis or
PostgreSQL, mixed as the settings choose): runs and their events, approvals, tool executions
and artifacts (files attached to them included), session memory and session owners, learned
memory, conversations with their messages, events and feedback, workflow runs, and the user's
MCP OAuth connections. ``build_container`` builds one as ``container.user_data``; the
``algen-agent-runtime users`` command and the ``/v1/admin/users/{user_id}`` routes use it.

Erasure refuses (``ConflictError``, nothing deleted) while the user has work in progress: a run
or workflow run that has not finished, a conversation turn in progress or waiting, or a tool call
whose outcome is unresolved (``started``, ``indeterminate``, ``compensating``,
``compensation_failed``), the same records the retention sweep keeps for operators. Cancel or
settle them first. ``dry_run`` reports what would go and what blocks it, without deleting.

Children go before their parents (artifacts, tool executions, approvals and events before the
runs they reference), so a failure part-way leaves no dangling reference, and running the
erasure again finishes it: every step deletes what is still there and counts only that.

Audit events are kept: they are the record of who did what (the erasure itself is audited) and
the audit retention window removes them. Usage quotas follow the deletions (PostgreSQL triggers
and the in-memory stores' own accounting).
"""

from __future__ import annotations

import base64
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.exceptions.errors import ConflictError, UserDataUnsupportedError
from algen_agent_runtime.types.contracts import TERMINAL_STATUSES, RunState, utc_now

# Every kind of record, in the order erasure removes them (children before parents).
USER_DATA_KINDS = (
    "conversation_feedback",
    "conversation_events",
    "conversation_messages",
    "conversations",
    "artifacts",
    "tool_executions",
    "approvals",
    "run_events",
    "runs",
    "workflow_runs",
    "session_memory",
    "session_owners",
    "learned_memory",
    "mcp_connections",
)
_UNRESOLVED_TOOL_STATUSES = frozenset(
    {"started", "indeterminate", "compensating", "compensation_failed"}
)
_BUSY_MESSAGE_STATUSES = frozenset(
    {"accepted", "streaming", "awaiting_approval", "awaiting_input", "awaiting_credentials"}
)
_FINISHED_WORKFLOW_STATUSES = frozenset({"completed", "failed", "cancelled"})
_PAGE = 200
_MEMORY_LIMIT = 1_000_000


class UserDataReport(BaseModel):
    """What ``erase_user`` deleted (or, with ``dry_run``, would delete), per kind.

    ``blocked`` lists what stops an erasure (only a dry run returns it non-empty: an erasure
    raises instead). ``retained`` names what is deliberately kept, such as audit events.
    """

    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    user_id: str
    dry_run: bool
    counts: dict[str, int] = Field(default_factory=dict)
    blocked: list[str] = Field(default_factory=list)
    retained: list[str] = Field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


class _Found:
    """One user's records, found across the stores."""

    def __init__(self) -> None:
        self.runs: list[RunState] = []
        self.workflows: list[Any] = []
        self.conversations: list[dict[str, Any]] = []
        self.sessions: dict[str, list[Any]] = {}
        self.owned_sessions: set[str] = set()
        self.artifacts: dict[str, Any] = {}
        self.events: list[Any] = []
        self.approvals: list[Any] = []
        self.tool_executions: list[Any] = []
        self.learned_memory: list[Any] = []
        self.mcp_servers: list[str] = []
        self.blocked: list[str] = []

    @property
    def run_ids(self) -> list[str]:
        return [state.id for state in self.runs]


def _require(store: Any, name: str, *methods: str) -> None:
    missing = [method for method in methods if not callable(getattr(store, method, None))]
    if missing:
        raise UserDataUnsupportedError(
            f"the {name} store {type(store).__qualname__} cannot export or erase one user's data "
            f"(it has no {', '.join(missing)}); the built-in in-memory, Redis and PostgreSQL "
            "stores support it"
        )


def _dump(value: Any) -> Any:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


class UserDataService:
    """Export or erase one user's data across the Runtime's stores.

    Pass the stores the deployment uses; ``None`` for one it does not have (a deployment without
    workflows or MCP OAuth, say). Every store given must support user data (the built-in ones
    do); otherwise ``UserDataUnsupportedError`` is raised before anything is read or deleted.
    """

    def __init__(
        self,
        *,
        runs: Any,
        memory: Any,
        events: Any,
        approvals: Any,
        tool_executions: Any,
        artifacts: Any,
        learned_memory: Any | None = None,
        conversations: Any | None = None,
        conversation_events: Any | None = None,
        conversation_feedback: Any | None = None,
        workflow_checkpoints: Any | None = None,
        mcp_tokens: Any | None = None,
        audits: Any | None = None,
    ) -> None:
        self._runs = runs
        self._memory = memory
        self._events = events
        self._approvals = approvals
        self._tool_executions = tool_executions
        self._artifacts = artifacts
        self._learned_memory = learned_memory
        self._conversations = conversations
        self._conversation_events = conversation_events
        self._conversation_feedback = conversation_feedback
        self._workflows = workflow_checkpoints
        self._mcp_tokens = mcp_tokens
        self._audits = audits

    def _check_supported(self) -> None:
        _require(self._runs, "run", "list_for_user", "delete_many")
        _require(
            self._memory, "memory", "sessions_for_user", "session_owner", "delete_session", "get"
        )
        _require(self._events, "event", "history", "forget_runs")
        _require(self._approvals, "approval", "list_for_runs", "forget_runs")
        _require(self._tool_executions, "tool execution", "list_for_runs", "forget_runs")
        _require(self._artifacts, "artifact", "list", "describe", "get", "delete")
        if self._learned_memory is not None:
            _require(self._learned_memory, "learned memory", "list_for_user", "forget_user")
        if self._conversations is not None:
            _require(self._conversations, "conversation", "list", "messages", "delete")
        if self._conversation_events is not None:
            _require(self._conversation_events, "conversation event", "history")
        if self._conversation_feedback is not None:
            _require(self._conversation_feedback, "conversation feedback", "list_for_conversation")
        if self._workflows is not None:
            _require(self._workflows, "workflow checkpoint", "list_for_user", "delete_many")
        if self._mcp_tokens is not None:
            _require(self._mcp_tokens, "MCP OAuth token", "servers_for_user", "forget_user")

    async def _find(self, tenant_id: str, user_id: str) -> _Found:
        found = _Found()
        found.runs = list(await self._runs.list_for_user(tenant_id, user_id))
        artifact_ids: list[str] = []
        sessions: list[str] = []
        for state in found.runs:
            if state.status not in TERMINAL_STATUSES:
                found.blocked.append(f"run {state.id} is {state.status.value}: cancel it first")
            sessions.extend(s for s in (state.session_id, state.request.session_id) if s)
            artifact_ids.extend(item.artifact_id for item in state.request.attachments)
            artifact_ids.extend(state.context_artifact_ids)
        if self._workflows is not None:
            found.workflows = list(await self._workflows.list_for_user(tenant_id, user_id))
            for workflow in found.workflows:
                if str(workflow.status) not in _FINISHED_WORKFLOW_STATUSES:
                    found.blocked.append(
                        f"workflow run {workflow.id} is {workflow.status}: cancel it first"
                    )
                artifact_ids.extend(item.artifact_id for item in workflow.attachments)
        if self._conversations is not None:
            before = None
            while True:
                page = list(await self._conversations.list(tenant_id, user_id, _PAGE, before))
                for conversation in page:
                    found.conversations.append(
                        await self._conversation(tenant_id, user_id, conversation, found)
                    )
                    sessions.append(conversation.id)
                    for message in found.conversations[-1]["messages"]:
                        artifact_ids.extend(message.artifacts)
                if len(page) < _PAGE:
                    break
                before = page[-1].updated_at
        run_ids = found.run_ids
        found.events = [event for run_id in run_ids for event in await self._events.history(run_id)]
        found.approvals = list(await self._approvals.list_for_runs(tenant_id, run_ids))
        found.tool_executions = list(await self._tool_executions.list_for_runs(tenant_id, run_ids))
        for record in found.tool_executions:
            if str(record.status) in _UNRESOLVED_TOOL_STATUSES:
                found.blocked.append(
                    f"tool call {record.idempotency_key} of run {record.run_id} is "
                    f"{record.status}: resolve it first (docs/operations.md"
                    "#resolving-indeterminate-tool-calls)"
                )
        for run_id in run_ids:
            for descriptor in await self._artifacts.list(tenant_id, run_id=run_id, limit=10_000):
                found.artifacts[descriptor.id] = descriptor
        for artifact_id in dict.fromkeys(artifact_ids):
            if artifact_id not in found.artifacts:
                descriptor = await self._artifacts.describe(artifact_id, tenant_id)
                if descriptor is not None:
                    found.artifacts[artifact_id] = descriptor
        found.owned_sessions = set(await self._memory.sessions_for_user(tenant_id, user_id))
        for session_id in dict.fromkeys([*sorted(found.owned_sessions), *sessions]):
            if session_id not in found.owned_sessions:
                owner = await self._memory.session_owner(tenant_id, session_id)
                if owner is not None and owner != user_id:
                    continue  # another user's session (a legacy run that named it)
            found.sessions[session_id] = list(
                await self._memory.get(tenant_id, session_id, _MEMORY_LIMIT)
            )
        if self._learned_memory is not None:
            found.learned_memory = list(
                await self._learned_memory.list_for_user(tenant_id, user_id)
            )
        if self._mcp_tokens is not None:
            found.mcp_servers = list(await self._mcp_tokens.servers_for_user(tenant_id, user_id))
        return found

    async def _conversation(
        self, tenant_id: str, user_id: str, conversation: Any, found: _Found
    ) -> dict[str, Any]:
        messages: list[Any] = []
        before = None
        while True:
            page = list(
                await self._conversations.messages(conversation.id, tenant_id, _PAGE, before)  # type: ignore[union-attr]
            )
            messages[:0] = page
            if len(page) < _PAGE:
                break
            before = page[0].created_at
        for message in messages:
            if str(message.status) in _BUSY_MESSAGE_STATUSES:
                found.blocked.append(
                    f"conversation {conversation.id} has a turn that is {message.status}: "
                    "cancel or answer it first"
                )
                break
        events = (
            list(await self._conversation_events.history(conversation.id))
            if self._conversation_events is not None
            else []
        )
        feedback = (
            list(
                await self._conversation_feedback.list_for_conversation(
                    tenant_id, user_id, conversation.id
                )
            )
            if self._conversation_feedback is not None
            else []
        )
        return {
            "conversation": conversation,
            "messages": messages,
            "events": events,
            "feedback": feedback,
        }

    async def export_user(
        self,
        tenant_id: str,
        user_id: str,
        *,
        include_artifact_data: bool = False,
        actor_id: str = "runtime.user_data",
    ) -> dict[str, Any]:
        """Everything stored about ``user_id`` in the tenant, as JSON-serialisable data.

        One dict, built in memory: ``runs`` (each run's state, checkpointed messages included),
        ``run_events``, ``approvals``, ``tool_executions``, ``artifacts`` (descriptors; with
        ``include_artifact_data`` each also has ``data_base64``), ``session_memory`` (per
        session), ``learned_memory`` (per agent), ``conversations`` (each with its ``messages``,
        ``events`` and ``feedback``), ``workflow_runs`` and ``mcp_connections`` (server names;
        tokens are never exported). Expired artifacts and memory are left out, as readers skip
        them.
        """
        self._check_supported()
        found = await self._find(tenant_id, user_id)
        artifacts: list[dict[str, Any]] = []
        for descriptor in found.artifacts.values():
            item = _dump(descriptor)
            if include_artifact_data:
                artifact = await self._artifacts.get(descriptor.id, tenant_id)
                item["data_base64"] = (
                    base64.b64encode(artifact.data).decode("ascii") if artifact else None
                )
            artifacts.append(item)
        from algen_agent_runtime import __version__

        exported = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "exported_at": utc_now().isoformat(),
            "runtime_version": __version__,
            "runs": [_dump(state) for state in found.runs],
            "run_events": [_dump(event) for event in found.events],
            "approvals": [_dump(item) for item in found.approvals],
            "tool_executions": [_dump(item) for item in found.tool_executions],
            "artifacts": artifacts,
            "session_memory": [
                {"session_id": session_id, "messages": [_dump(m) for m in messages]}
                for session_id, messages in found.sessions.items()
            ],
            "learned_memory": [_dump(item) for item in found.learned_memory],
            "conversations": [
                {
                    key: [_dump(v) for v in value] if isinstance(value, list) else _dump(value)
                    for key, value in item.items()
                }
                for item in found.conversations
            ],
            "workflow_runs": [_dump(item) for item in found.workflows],
            "mcp_connections": [{"server": server} for server in found.mcp_servers],
        }
        await self._audit("user_data.exported", tenant_id, user_id, actor_id, {})
        return exported

    async def erase_user(
        self,
        tenant_id: str,
        user_id: str,
        *,
        dry_run: bool = False,
        actor_id: str = "runtime.user_data",
    ) -> UserDataReport:
        """Delete everything stored about ``user_id`` in the tenant; return counts per kind.

        Raises ``ConflictError`` (deleting nothing) while the user has work in progress (see the
        module docstring); ``dry_run`` returns the counts and ``blocked`` instead. Idempotent: a
        second call, or one after a failure part-way, deletes what is left.
        """
        self._check_supported()
        found = await self._find(tenant_id, user_id)
        report = UserDataReport(
            tenant_id=tenant_id,
            user_id=user_id,
            dry_run=dry_run,
            retained=["audit_events"],
        )
        if dry_run:
            report.blocked = found.blocked
            report.counts = self._found_counts(found)
            return report
        if found.blocked:
            raise ConflictError(
                f"user {user_id!r} has work in progress; nothing was erased: "
                + "; ".join(found.blocked)
            )
        counts = dict.fromkeys(USER_DATA_KINDS, 0)
        conversation_ids = [item["conversation"].id for item in found.conversations]
        for item in found.conversations:
            if await self._conversations.delete(item["conversation"].id, tenant_id):  # type: ignore[union-attr]
                counts["conversations"] += 1
                counts["conversation_messages"] += len(item["messages"])
                # PostgreSQL deletes the conversation's events and feedback with it.
                if not callable(getattr(self._conversation_events, "forget", None)):
                    counts["conversation_events"] += len(item["events"])
                if not callable(getattr(self._conversation_feedback, "forget_conversations", None)):
                    counts["conversation_feedback"] += len(item["feedback"])
        forget_events = getattr(self._conversation_events, "forget", None)
        if callable(forget_events):
            counts["conversation_events"] += await forget_events(conversation_ids)
        forget_feedback = getattr(self._conversation_feedback, "forget_conversations", None)
        if callable(forget_feedback):
            counts["conversation_feedback"] += await forget_feedback(conversation_ids)
        for artifact_id in found.artifacts:
            if await self._artifacts.delete(artifact_id, tenant_id):
                counts["artifacts"] += 1
        run_ids = found.run_ids
        counts["tool_executions"] += await self._tool_executions.forget_runs(tenant_id, run_ids)
        counts["approvals"] += await self._approvals.forget_runs(tenant_id, run_ids)
        counts["run_events"] += await self._events.forget_runs(run_ids)
        deleted = await self._runs.delete_many(tenant_id, run_ids)
        # The PostgreSQL run store also removes children written since they were deleted above.
        for kind, key in (
            ("runs", "runs"),
            ("run_events", "events"),
            ("approvals", "approvals"),
            ("tool_executions", "tool_executions"),
        ):
            counts[kind] += int(deleted.get(key, 0))
        if self._workflows is not None:
            counts["workflow_runs"] += await self._workflows.delete_many(
                tenant_id, [workflow.id for workflow in found.workflows]
            )
        for session_id in found.sessions:
            owned = session_id in found.owned_sessions
            counts["session_memory"] += await self._memory.delete_session(tenant_id, session_id)
            counts["session_owners"] += int(owned)
        if self._learned_memory is not None:
            counts["learned_memory"] += await self._learned_memory.forget_user(tenant_id, user_id)
        if self._mcp_tokens is not None:
            counts["mcp_connections"] += await self._mcp_tokens.forget_user(tenant_id, user_id)
        report.counts = counts
        await self._audit("user_data.erased", tenant_id, user_id, actor_id, {"counts": counts})
        return report

    @staticmethod
    def _found_counts(found: _Found) -> dict[str, int]:
        return {
            "conversation_feedback": sum(len(item["feedback"]) for item in found.conversations),
            "conversation_events": sum(len(item["events"]) for item in found.conversations),
            "conversation_messages": sum(len(item["messages"]) for item in found.conversations),
            "conversations": len(found.conversations),
            "artifacts": len(found.artifacts),
            "tool_executions": len(found.tool_executions),
            "approvals": len(found.approvals),
            "run_events": len(found.events),
            "runs": len(found.runs),
            "workflow_runs": len(found.workflows),
            "session_memory": sum(len(messages) for messages in found.sessions.values()),
            "session_owners": len(found.owned_sessions),
            "learned_memory": len(found.learned_memory),
            "mcp_connections": len(found.mcp_servers),
        }

    async def _audit(
        self, action: str, tenant_id: str, user_id: str, actor_id: str, metadata: dict[str, Any]
    ) -> None:
        if self._audits is None:
            return
        from algen_agent_runtime.events.contracts import AuditEvent

        await self._audits.append(
            AuditEvent(
                action=action,
                outcome="completed",
                tenant_id=tenant_id,
                actor_id=actor_id,
                resource_id=user_id,
                metadata=metadata,
            )
        )

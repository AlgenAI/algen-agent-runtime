"""A session belongs to the user who first used it: another user's run cannot read or extend it."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import httpx
import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.conversations.service import (
    ConversationHandlerRegistry,
    ConversationService,
    RuntimeConversationHandler,
)
from algen_agent_runtime.conversations.stores import (
    InMemoryConversationEventBus,
    InMemoryConversationStore,
)
from algen_agent_runtime.exceptions.errors import NotFoundError
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.orchestration.container import Container
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore, InMemoryMemoryStore
from algen_agent_runtime.types.contracts import MemoryPolicy, Message, RunRequest, RunStatus
from algen_agent_runtime.types.interfaces import SessionOwnership

PRIVATE_NOTE = "alice-private-7c1d"


def _request(user: str, session_id: str | None = None, text: str = "hello") -> RunRequest:
    return RunRequest(
        agent="test-agent", input=text, tenant_id="t", user_id=user, session_id=session_id
    )


def _seen(provider: MockModelProvider) -> str:
    return provider.requests[-1].model_dump_json()


async def test_the_in_memory_store_claims_a_session_for_its_first_user() -> None:
    store = InMemoryMemoryStore()

    assert isinstance(store, SessionOwnership)
    assert await store.claim_session("t", "s1", "alice")
    assert await store.claim_session("t", "s1", "alice")
    assert not await store.claim_session("t", "s1", "bob")
    # Another tenant's session of the same id is a different session.
    assert await store.claim_session("t2", "s1", "bob")


async def test_another_users_run_cannot_name_the_session() -> None:
    provider = MockModelProvider([PRIVATE_NOTE, "bob's answer", "alice again"])
    runtime = make_runtime(provider)

    first = await runtime.run(_request("alice", "s1", f"remember {PRIVATE_NOTE}"))
    assert first.status == RunStatus.COMPLETED, first.error

    with pytest.raises(NotFoundError, match="session 's1' not found"):
        await runtime.start(_request("bob", "s1", "what did alice say?"))
    assert len(provider.requests) == 1  # Bob's run never reached the model

    again = await runtime.run(_request("alice", "s1", "and now?"))
    assert again.status == RunStatus.COMPLETED, again.error
    assert PRIVATE_NOTE in _seen(provider)  # the owner still gets her history


async def test_a_generated_session_is_claimed_when_its_memory_is_written() -> None:
    provider = MockModelProvider([PRIVATE_NOTE])
    runtime = make_runtime(provider)

    first = await runtime.run(_request("alice"))

    with pytest.raises(NotFoundError):
        await runtime.start(_request("bob", first.session_id))


async def test_a_session_is_claimed_even_when_the_agent_keeps_no_memory() -> None:
    # The session also keys other state (a session-scoped tool-result cache, say).
    runtime = make_runtime(
        MockModelProvider(["one", "two"]),
        make_agent(memory_policy=MemoryPolicy(enabled=False)),
    )
    await runtime.run(_request("alice", "s1"))

    with pytest.raises(NotFoundError):
        await runtime.start(_request("bob", "s1"))


async def test_a_lost_race_for_a_generated_session_skips_the_memory_write() -> None:
    provider = MockModelProvider(["answer"])
    runtime = make_runtime(provider)
    store = runtime.memory
    assert isinstance(store, InMemoryMemoryStore)
    state = await runtime.start(_request("alice"))
    # Another user takes the generated id before Alice's run writes its memory.
    await store.claim_session("t", state.session_id, "mallory")

    result = await runtime.wait(state.id, "t")

    assert result.status == RunStatus.COMPLETED, result.error
    assert await store.get("t", state.session_id, 10) == ()
    [written] = [
        event for event in await runtime.events.history(state.id) if event.type == "memory.written"
    ]
    assert written.data["operation"] == "skipped"
    assert written.data["reason"] == "session_owned_by_another_user"


class _UserBoundMemory:
    """A custom store that isolates users itself (it has no ``claim_session``)."""

    def __init__(self) -> None:
        self.appended: list[tuple[str, str]] = []

    async def append(self, tenant_id: str, session_id: str, messages: Sequence[Message]) -> None:
        self.appended.append((tenant_id, session_id))

    async def get(self, tenant_id: str, session_id: str, limit: int) -> Sequence[Message]:
        return ()

    async def delete(self, tenant_id: str, session_id: str) -> None:
        return None


async def test_a_custom_store_without_claims_is_trusted_to_isolate_users() -> None:
    runtime = make_runtime(MockModelProvider(["one", "two"]))
    custom = _UserBoundMemory()
    runtime.memory = custom

    await runtime.run(_request("alice", "s1"))
    second = await runtime.run(_request("bob", "s1"))

    assert second.status == RunStatus.COMPLETED, second.error
    assert custom.appended == [("t", "s1"), ("t", "s1")]


def _headers(user: str, scopes: str = "runs:read runs:write") -> dict[str, str]:
    return {"x-tenant-id": "t", "x-user-id": user, "x-scopes": scopes}


def _container(provider: MockModelProvider) -> Container:
    runtime = make_runtime(provider)
    handlers = ConversationHandlerRegistry()
    handlers.register(RuntimeConversationHandler(runtime))
    return Container(
        runtime=runtime,
        agents=runtime.agents,
        tools=runtime.tools,
        artifacts=InMemoryArtifactStore(),
        events=runtime.events,
        conversations=ConversationService(
            InMemoryConversationStore(), InMemoryConversationEventBus(), handlers
        ),
    )


async def _finished(container: Container, run_id: str) -> None:
    for _ in range(400):
        state = await container.runtime.status(run_id, "t")
        if state.status == RunStatus.COMPLETED:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"run {run_id} did not complete")


async def test_http_runs_with_another_users_session_get_404() -> None:
    provider = MockModelProvider([PRIVATE_NOTE, "alice again"])
    container = _container(provider)
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body: dict[str, Any] = {"agent": "test-agent", "input": "hi", "session_id": "s1"}
        alice = await client.post("/v1/runs", headers=_headers("alice"), json=body)
        assert alice.status_code == 202, alice.text
        await _finished(container, alice.json()["run_id"])

        bob = await client.post("/v1/runs", headers=_headers("bob"), json=body)
        assert bob.status_code == 404
        assert bob.json() == {"detail": "session 's1' not found"}

        again = await client.post("/v1/runs", headers=_headers("alice"), json=body)
        assert again.status_code == 202, again.text
        await _finished(container, again.json()["run_id"])
    assert PRIVATE_NOTE in _seen(provider)


async def test_a_conversations_session_is_its_owners() -> None:
    provider = MockModelProvider([PRIVATE_NOTE, "second turn"])
    container = _container(provider)
    transport = httpx.ASGITransport(app=create_app(AppSettings(), container))
    scopes = "conversations:read conversations:write runs:read runs:write"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/conversations", headers=_headers("alice", scopes), json={"agent": "test-agent"}
        )
        conversation_id = created.json()["id"]
        for text in ("first", "second"):
            sent = await client.post(
                f"/v1/conversations/{conversation_id}/messages",
                headers=_headers("alice", scopes),
                json={"text": text},
            )
            assert sent.status_code == 202, sent.text
            for _ in range(400):
                messages = (
                    await client.get(
                        f"/v1/conversations/{conversation_id}/messages",
                        headers=_headers("alice", scopes),
                    )
                ).json()
                if messages[-1]["status"] == "completed":
                    break
                await asyncio.sleep(0.005)
            assert messages[-1]["status"] == "completed", messages[-1]

        # The conversation's turns kept their history: the second turn saw the first.
        assert PRIVATE_NOTE in _seen(provider)
        bob = await client.post(
            "/v1/runs",
            headers=_headers("bob"),
            json={"agent": "test-agent", "input": "hi", "session_id": conversation_id},
        )
        assert bob.status_code == 404

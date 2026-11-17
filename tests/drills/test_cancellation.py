"""Cancellation drills on PostgreSQL: a cancelled run stays cancelled across a restart.

The agent's model is a real ``OpenAICompatibleProvider`` talking to a local fake API
(``tests/fake_model_server.py``) that holds each call open and records when the client hangs up.

- Cancelled in its own process: the provider request is aborted, ``cancelled`` is what the
  database holds, and a new process's recovery does not drive the run again.
- Cancelled from another process (a second worker on the same database): the run is
  ``cancelled`` at once, but the call already in flight in the driving process is not aborted
  (cancellation is not delivered across processes). The call is answered and recorded
  (``model.completed``, with its usage, since the provider bills it); the drive then stands
  down at its next checkpoint, so the run stays cancelled and no further model or tool call
  starts.
- Shut down instead (a drain, not a cancel): the request is aborted too, but the run is kept for
  recovery, and the next process drives it again.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from conftest import make_agent
from drill_support import TENANT, USER, build, fresh_database, query, requires_postgres, until
from fake_model_server import FakeModelServer, TrackingTransport, tracked_client

from algen_agent_runtime.models.providers.openai_compatible import OpenAICompatibleProvider
from algen_agent_runtime.orchestration.container import Container
from algen_agent_runtime.types.contracts import (
    ModelCapabilities,
    ModelProfile,
    RunRequest,
    RunStatus,
)

pytestmark = [pytest.mark.drill, pytest.mark.integration, pytest.mark.postgres, requires_postgres]

FAKE_AGENT = "drill-fake-model-agent"
QUIET_SECONDS = 0.5


@asynccontextmanager
async def _process(
    dsn: str, server: FakeModelServer, client: httpx.AsyncClient
) -> AsyncIterator[Container]:
    """A Runtime process on ``dsn`` whose agent's model is the fake server; its start recovers
    what the database holds."""
    container, app = build(dsn)
    container.runtime.router.register_provider(
        OpenAICompatibleProvider(
            "fake",
            server.url,
            None,
            "fake-model",
            ModelCapabilities(streaming=True, tools=True),
            client=client,
        )
    )
    container.runtime.agents.register(
        make_agent(
            name=FAKE_AGENT, default_model=ModelProfile(name="f", provider="fake", model="fake")
        )
    )
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    try:
        yield container
    finally:
        await lifespan.__aexit__(None, None, None)


def _request(*, stream: bool = True) -> RunRequest:
    return RunRequest(
        agent=FAKE_AGENT, input="hello", tenant_id=TENANT, user_id=USER, stream=stream
    )


async def _stored_status(dsn: str, run_id: str) -> str:
    rows = await query(
        dsn, "SELECT state->>'status' AS status FROM algen_agent_runtime_runs WHERE id=$1", run_id
    )
    return str(rows[0]["status"])


async def _hung_up(server: FakeModelServer, transport: TrackingTransport) -> None:
    await asyncio.wait_for(server.last.disconnected.wait(), 5)
    assert not server.last.finished
    assert transport.open_responses == 0


async def _event_types(container: Container, run_id: str) -> list[str]:
    return [event.type for event in await container.runtime.events.history(run_id)]


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "plain"])
async def test_a_cancelled_run_stays_cancelled_after_a_restart(stream: bool) -> None:
    client, transport = tracked_client()
    async with fresh_database("cancel") as dsn, FakeModelServer() as server:
        async with _process(dsn, server, client) as first:
            state = await first.runtime.start(_request(stream=stream))
            await server.wait_for_request()

            await first.runtime.cancel(state.id, TENANT)
            result = await first.runtime.wait(state.id, TENANT)

            assert result.status == RunStatus.CANCELLED
            await _hung_up(server, transport)
        assert await _stored_status(dsn, state.id) == "cancelled"

        async with _process(dsn, server, client) as second:
            await asyncio.sleep(QUIET_SECONDS)
            # Recovery left it alone: no new model call, still cancelled, cancelled once.
            assert len(server.exchanges) == 1
            assert (await second.runtime.status(state.id, TENANT)).status == RunStatus.CANCELLED
            events = await _event_types(second, state.id)
            assert events.count("run.cancelled") == 1
            assert events[-1] == "run.cancelled"
        assert await _stored_status(dsn, state.id) == "cancelled"
    await client.aclose()


async def test_a_run_cancelled_from_another_process_stops_at_its_next_checkpoint() -> None:
    client, _ = tracked_client()
    # Each call is answered after a second: long enough to cancel it while it is in flight.
    async with fresh_database("cancel") as dsn, FakeModelServer(delay=1.0) as server:
        async with _process(dsn, server, client) as driver, _process(dsn, server, client) as other:
            state = await driver.runtime.start(_request(stream=False))
            await server.wait_for_request()

            cancelled = await other.runtime.cancel(state.id, TENANT)

            assert cancelled.status == RunStatus.CANCELLED
            assert await _stored_status(dsn, state.id) == "cancelled"
            # The call in flight in the driving process is not aborted: it is answered...
            await until(lambda: server.last.finished, "the in-flight call to be answered", 5)
            # ...and recorded, as the provider bills it; then its drive stands down at the next
            # checkpoint, leaving the run cancelled.
            await driver.runtime.wait(state.id, TENANT)
            await asyncio.sleep(QUIET_SECONDS)
            assert len(server.exchanges) == 1
            assert await _stored_status(dsn, state.id) == "cancelled"
            events = await _event_types(other, state.id)
            assert events.count("run.cancelled") == 1
            assert events.count("model.completed") <= 1
            assert "run.completed" not in events
            assert not any(kind.startswith("tool.") for kind in events)
    await client.aclose()


async def test_a_shutdown_aborts_the_request_but_keeps_the_run_for_recovery() -> None:
    client, transport = tracked_client()
    async with fresh_database("cancel") as dsn, FakeModelServer() as server:
        async with _process(dsn, server, client) as first:
            state = await first.runtime.start(_request())
            await server.wait_for_request()
        # The lifespan's shutdown drained the run (grace 0.5 s), then interrupted it.
        await _hung_up(server, transport)
        assert await _stored_status(dsn, state.id) not in {"cancelled", "failed"}

        server.request_received.clear()
        async with _process(dsn, server, client) as second:
            # Recovery drives it again: a second model call reaches the server.
            await server.wait_for_request()
            assert len(server.exchanges) == 2
            await second.runtime.cancel(state.id, TENANT)
            assert (await second.runtime.wait(state.id, TENANT)).status == RunStatus.CANCELLED
            await _hung_up(server, transport)
        assert await _stored_status(dsn, state.id) == "cancelled"
    await client.aclose()


async def test_many_runs_cancelled_at_once_each_end_cancelled_once() -> None:
    # ``cancel`` and the drive it cancels both mark the run cancelled; on PostgreSQL their
    # writes interleave. Exactly one wins, neither fails, and the run is cancelled once.
    client, transport = tracked_client()
    async with fresh_database("cancel") as dsn, FakeModelServer() as server:
        async with _process(dsn, server, client) as process:
            runtime = process.runtime
            states = [await runtime.start(_request(stream=index % 2 == 0)) for index in range(12)]
            await until(lambda: len(server.exchanges) == len(states), "every model call", 10)

            cancelled = await asyncio.gather(
                *(runtime.cancel(state.id, TENANT) for state in states)
            )
            results = await asyncio.gather(*(runtime.wait(state.id, TENANT) for state in states))

            assert {state.status for state in cancelled} == {RunStatus.CANCELLED}
            assert {result.status for result in results} == {RunStatus.CANCELLED}
            for state in states:
                events = await _event_types(process, state.id)
                assert events.count("run.cancelled") == 1, events
                assert await _stored_status(dsn, state.id) == "cancelled"
            for exchange in server.exchanges:
                await asyncio.wait_for(exchange.disconnected.wait(), 5)
            assert transport.open_responses == 0
    await client.aclose()

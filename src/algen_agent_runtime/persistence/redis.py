from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from algen_agent_runtime.exceptions.errors import (
    ConflictError,
    RunConflictError,
    StoreUnavailableError,
)
from algen_agent_runtime.types.contracts import Message, RunState

# redis-py's errors for a server it cannot reach or that did not answer in time (redis is
# optional, so they are recognised by name).
_UNAVAILABLE_ERRORS = frozenset({"ConnectionError", "TimeoutError", "BusyLoadingError"})


def redis_unavailable(exc: BaseException) -> StoreUnavailableError | None:
    """The runtime error for a redis-py error that means Redis is unavailable, else None."""
    if isinstance(exc, StoreUnavailableError):
        return None
    names = {cls.__name__ for cls in type(exc).__mro__ if cls.__module__.startswith("redis")}
    if names & _UNAVAILABLE_ERRORS:
        return StoreUnavailableError(f"Redis is unavailable ({type(exc).__name__})")
    return None


@asynccontextmanager
async def _available() -> AsyncIterator[None]:
    try:
        yield
    except Exception as exc:
        unavailable = redis_unavailable(exc)
        if unavailable is not None:
            raise unavailable from exc
        raise


class RedisRunStore:
    def __init__(self, redis: Any, prefix: str = "algen-agent-runtime") -> None:
        self._redis = redis
        self._prefix = prefix

    def _key(self, tenant_id: str, run_id: str) -> str:
        return f"{self._prefix}:tenant:{tenant_id}:run:{run_id}"

    async def create(self, state: RunState) -> None:
        async with _available():
            created = await self._redis.set(
                self._key(state.request.tenant_id, state.id), state.model_dump_json(), nx=True
            )
            if not created:
                raise ConflictError(f"run {state.id!r} already exists")

    async def get(self, run_id: str, tenant_id: str) -> RunState | None:
        async with _available():
            value = await self._redis.get(self._key(tenant_id, run_id))
            return RunState.model_validate_json(value) if value else None

    async def save(self, state: RunState, expected_version: int) -> None:
        async with _available():
            key = self._key(state.request.tenant_id, state.id)
            async with self._redis.pipeline(transaction=True) as pipe:
                await pipe.watch(key)
                value = await pipe.get(key)
                current = RunState.model_validate_json(value) if value else None
                if current is None or current.version != expected_version:
                    raise RunConflictError(f"run {state.id!r} was concurrently modified")
                saved = state.model_copy(update={"version": expected_version + 1})
                pipe.multi()
                pipe.set(key, saved.model_dump_json())
                try:
                    await pipe.execute()
                except Exception as exc:
                    # redis-py raises WatchError when another writer touched the key after WATCH.
                    if type(exc).__name__ == "WatchError":
                        raise RunConflictError(
                            f"run {state.id!r} was concurrently modified"
                        ) from exc
                    raise
                state.version = saved.version

    async def list_active(self, limit: int = 1000) -> Sequence[RunState]:
        async with _available():
            from algen_agent_runtime.types.contracts import TERMINAL_STATUSES

            states: list[RunState] = []
            pattern = f"{self._prefix}:tenant:*:run:*"
            async for key in self._redis.scan_iter(match=pattern, count=min(limit, 1000)):
                value = await self._redis.get(key)
                if value:
                    state = RunState.model_validate_json(value)
                    if state.status not in TERMINAL_STATUSES:
                        states.append(state)
                        if len(states) >= limit:
                            break
            states.sort(key=lambda state: state.updated_at)
            return tuple(states)


class RedisMemoryStore:
    def __init__(
        self, redis: Any, prefix: str = "algen-agent-runtime", retention_seconds: int = 86400
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._retention = retention_seconds

    def _key(self, tenant_id: str, session_id: str) -> str:
        return f"{self._prefix}:tenant:{tenant_id}:session:{session_id}:memory"

    async def append(self, tenant_id: str, session_id: str, messages: Sequence[Message]) -> None:
        async with _available():
            key = self._key(tenant_id, session_id)
            if messages:
                await self._redis.rpush(key, *(message.model_dump_json() for message in messages))
                await self._redis.expire(key, self._retention)

    async def get(self, tenant_id: str, session_id: str, limit: int) -> Sequence[Message]:
        async with _available():
            values = await self._redis.lrange(self._key(tenant_id, session_id), -limit, -1)
            return tuple(Message.model_validate_json(value) for value in values)

    async def delete(self, tenant_id: str, session_id: str) -> None:
        async with _available():
            await self._redis.delete(self._key(tenant_id, session_id))

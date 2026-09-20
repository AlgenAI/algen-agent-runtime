from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.types.contracts import Message, RunState


class RedisRunStore:
    def __init__(self, redis: Any, prefix: str = "algen-agent-runtime") -> None:
        self._redis = redis
        self._prefix = prefix

    def _key(self, tenant_id: str, run_id: str) -> str:
        return f"{self._prefix}:tenant:{tenant_id}:run:{run_id}"

    async def create(self, state: RunState) -> None:
        created = await self._redis.set(
            self._key(state.request.tenant_id, state.id), state.model_dump_json(), nx=True
        )
        if not created:
            raise ConflictError(f"run {state.id!r} already exists")

    async def get(self, run_id: str, tenant_id: str) -> RunState | None:
        value = await self._redis.get(self._key(tenant_id, run_id))
        return RunState.model_validate_json(value) if value else None

    async def save(self, state: RunState, expected_version: int) -> None:
        key = self._key(state.request.tenant_id, state.id)
        async with self._redis.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(key)
                    value = await pipe.get(key)
                    current = RunState.model_validate_json(value) if value else None
                    if current is None or current.version != expected_version:
                        raise ConflictError(f"run {state.id!r} was concurrently modified")
                    saved = state.model_copy(update={"version": expected_version + 1})
                    pipe.multi()
                    pipe.set(key, saved.model_dump_json())
                    await pipe.execute()
                    state.version = saved.version
                    return
                except ConflictError:
                    raise

    async def list_active(self, limit: int = 1000) -> Sequence[RunState]:
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
        key = self._key(tenant_id, session_id)
        if messages:
            await self._redis.rpush(key, *(message.model_dump_json() for message in messages))
            await self._redis.expire(key, self._retention)

    async def get(self, tenant_id: str, session_id: str, limit: int) -> Sequence[Message]:
        values = await self._redis.lrange(self._key(tenant_id, session_id), -limit, -1)
        return tuple(Message.model_validate_json(value) for value in values)

    async def delete(self, tenant_id: str, session_id: str) -> None:
        await self._redis.delete(self._key(tenant_id, session_id))

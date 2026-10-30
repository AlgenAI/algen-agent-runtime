from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from algen_agent_runtime.exceptions.errors import (
    ConflictError,
    QuotaExceededError,
    RunConflictError,
    StoreUnavailableError,
)
from algen_agent_runtime.persistence.quotas import ResourceUsage, StorageQuotas
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


# Session memory with usage accounting, atomically. KEYS: the session list, the tenant's usage
# hash (items, bytes), a sorted set of the tenant's sessions by expiry time, and a hash of each
# session's items and bytes. Sessions whose list has expired are first subtracted from the usage.
# ARGV: ttl ms, item limit, byte limit (-1 for none), items and bytes to add, then the messages.
_APPEND_MEMORY = """
local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local ttl = tonumber(ARGV[1])
local max_items = tonumber(ARGV[2])
local max_bytes = tonumber(ARGV[3])
local add_items = tonumber(ARGV[4])
local add_bytes = tonumber(ARGV[5])
local expired = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', now, 'LIMIT', 0, 256)
for _, session in ipairs(expired) do
  local size = redis.call('HMGET', KEYS[4], session .. '|items', session .. '|bytes')
  redis.call('HINCRBY', KEYS[2], 'items', -(tonumber(size[1]) or 0))
  redis.call('HINCRBY', KEYS[2], 'bytes', -(tonumber(size[2]) or 0))
  redis.call('HDEL', KEYS[4], session .. '|items', session .. '|bytes')
  redis.call('ZREM', KEYS[3], session)
end
local items = tonumber(redis.call('HGET', KEYS[2], 'items') or '0')
local bytes = tonumber(redis.call('HGET', KEYS[2], 'bytes') or '0')
if add_items == 0 then
  return {1, items, bytes}
end
if max_items >= 0 and items + add_items > max_items then
  return {0, 'items', items + add_items}
end
if max_bytes >= 0 and bytes + add_bytes > max_bytes then
  return {0, 'bytes', bytes + add_bytes}
end
for i = 6, #ARGV do
  redis.call('RPUSH', KEYS[1], ARGV[i])
end
redis.call('PEXPIRE', KEYS[1], ttl)
redis.call('HINCRBY', KEYS[2], 'items', add_items)
redis.call('HINCRBY', KEYS[2], 'bytes', add_bytes)
redis.call('HINCRBY', KEYS[4], KEYS[1] .. '|items', add_items)
redis.call('HINCRBY', KEYS[4], KEYS[1] .. '|bytes', add_bytes)
redis.call('ZADD', KEYS[3], now + ttl, KEYS[1])
return {1, items + add_items, bytes + add_bytes}
"""

_DELETE_MEMORY = """
local size = redis.call('HMGET', KEYS[4], KEYS[1] .. '|items', KEYS[1] .. '|bytes')
if size[1] then
  redis.call('HINCRBY', KEYS[2], 'items', -tonumber(size[1]))
  redis.call('HINCRBY', KEYS[2], 'bytes', -(tonumber(size[2]) or 0))
  redis.call('HDEL', KEYS[4], KEYS[1] .. '|items', KEYS[1] .. '|bytes')
  redis.call('ZREM', KEYS[3], KEYS[1])
end
return redis.call('DEL', KEYS[1])
"""


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


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
    """Session memory lists with a TTL.

    With ``quotas``, appends and deletes run as Lua scripts that keep each tenant's usage beside
    its sessions, so a quota is checked atomically; the scripts touch several keys, so this needs
    a Redis that is not clustered. Sessions written before quotas were configured are not
    counted until they expire.
    """

    def __init__(
        self,
        redis: Any,
        prefix: str = "algen-agent-runtime",
        retention_seconds: int = 86400,
        quotas: StorageQuotas | None = None,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._retention = retention_seconds
        self._quotas = quotas

    def _key(self, tenant_id: str, session_id: str) -> str:
        return f"{self._prefix}:tenant:{tenant_id}:session:{session_id}:memory"

    def _usage_keys(self, tenant_id: str) -> tuple[str, str, str]:
        usage = f"{self._prefix}:tenant:{tenant_id}:usage:memory"
        return usage, f"{usage}:sessions", f"{usage}:sizes"

    async def append(self, tenant_id: str, session_id: str, messages: Sequence[Message]) -> None:
        async with _available():
            key = self._key(tenant_id, session_id)
            if not messages:
                return
            values = [message.model_dump_json() for message in messages]
            if self._quotas is None:
                await self._redis.rpush(key, *values)
                await self._redis.expire(key, self._retention)
                return
            limit = self._quotas.limit(tenant_id, "memory")
            result = await self._redis.eval(
                _APPEND_MEMORY,
                4,
                key,
                *self._usage_keys(tenant_id),
                max(1, self._retention) * 1000,
                -1 if limit.items is None else limit.items,
                -1 if limit.bytes is None else limit.bytes,
                len(values),
                sum(len(value.encode()) for value in values),
                *values,
            )
            if int(result[0]) == 0:
                dimension = _text(result[1])
                maximum = limit.items if dimension == "items" else limit.bytes
                raise QuotaExceededError("memory", dimension, int(maximum or 0), int(result[2]))

    async def tenant_usage(self, tenant_id: str) -> ResourceUsage:
        """Usage of the tenant's unexpired sessions; zero without ``quotas``."""
        if self._quotas is None:
            return ResourceUsage()
        async with _available():
            result = await self._redis.eval(
                _APPEND_MEMORY, 4, "", *self._usage_keys(tenant_id), 0, -1, -1, 0, 0
            )
        return ResourceUsage(int(result[1]), int(result[2]))

    async def get(self, tenant_id: str, session_id: str, limit: int) -> Sequence[Message]:
        async with _available():
            values = await self._redis.lrange(self._key(tenant_id, session_id), -limit, -1)
            return tuple(Message.model_validate_json(value) for value in values)

    async def delete(self, tenant_id: str, session_id: str) -> None:
        async with _available():
            key = self._key(tenant_id, session_id)
            if self._quotas is None:
                await self._redis.delete(key)
                return
            await self._redis.eval(_DELETE_MEMORY, 4, key, *self._usage_keys(tenant_id))

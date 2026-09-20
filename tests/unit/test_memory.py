from algen_agent_runtime.persistence.memory import InMemoryMemoryStore
from algen_agent_runtime.types.contracts import Message, Role


async def test_memory_is_tenant_isolated() -> None:
    store = InMemoryMemoryStore()
    await store.append("tenant-a", "same-session", [Message.text(Role.USER, "private")])
    assert await store.get("tenant-b", "same-session", 10) == ()
    assert (await store.get("tenant-a", "same-session", 10))[0].text_content == "private"


async def test_memory_deletion() -> None:
    store = InMemoryMemoryStore()
    await store.append("tenant", "session", [Message.text(Role.USER, "delete")])
    await store.delete("tenant", "session")
    assert await store.get("tenant", "session", 10) == ()

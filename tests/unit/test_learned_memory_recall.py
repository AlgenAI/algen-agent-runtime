"""Semantic and hybrid learned-memory recall (learned_memory_policy.recall)."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import timedelta

import pytest
from conftest import make_agent, make_runtime
from pydantic import ValidationError

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.context.packing import estimate_message
from algen_agent_runtime.exceptions.errors import ConfigurationError, ToolExecutionError
from algen_agent_runtime.memory.contracts import MemoryEmbedding, MemoryScope
from algen_agent_runtime.memory.service import MEMORY_DELETE, MEMORY_RECALL, MEMORY_WRITE
from algen_agent_runtime.memory.store import InMemoryLearnedMemoryStore
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.tools.contracts import ToolContext
from algen_agent_runtime.types.contracts import (
    ApprovalPolicy,
    LearnedMemoryPolicy,
    Message,
    Role,
    RunRequest,
    RunState,
    RunStatus,
    TextBlock,
    utc_now,
)

# Words mapped to concepts, so paraphrases share a direction and unrelated text does not.
_CONCEPTS = {
    "invoice": 0,
    "invoices": 0,
    "billing": 0,
    "bill": 0,
    "statement": 0,
    "layout": 1,
    "format": 1,
    "formatting": 1,
    "template": 1,
    "weather": 2,
    "rain": 2,
    "forecast": 2,
    "recipe": 3,
    "cooking": 3,
    "pasta": 3,
}
_DIMENSIONS = 8


class ConceptEmbeddings(MockModelProvider):
    """A deterministic embedding provider: one dimension per concept, the rest per word."""

    provider_id = "embeddings"

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.calls: list[tuple[list[str], str | None]] = []
        self.fail = fail

    async def embed(self, texts: Sequence[str], model: str | None = None) -> list[list[float]]:
        self.calls.append((list(texts), model))
        if self.fail:
            raise RuntimeError("provider unavailable")
        vectors = []
        for text in texts:
            vector = [0.0] * _DIMENSIONS
            for word in re.findall(r"[a-z]+", text.lower()):
                index = _CONCEPTS.get(word)
                if index is not None:
                    vector[index] += 1.0
                else:
                    vector[4 + sum(map(ord, word)) % 4] += 0.2
            vectors.append(vector)
        return vectors


def policy(**updates):
    return LearnedMemoryPolicy(
        **{
            "enabled": True,
            "recall": "semantic",
            "embedding_provider": "embeddings",
            "embedding_model": "concepts",
            **updates,
        }
    )


def agent(**updates):
    return make_agent(
        **{
            "enabled_tools": {MEMORY_WRITE, MEMORY_RECALL, MEMORY_DELETE},
            "tool_permissions": {"memory.write", "memory.read", "memory.delete"},
            "learned_memory_policy": policy(),
            "approval_policy": ApprovalPolicy(
                require_for_side_effects=False, require_for_destructive=False
            ),
            **updates,
        }
    )


def request(**updates):
    return RunRequest(
        **{"agent": "test-agent", "input": "Remember", "tenant_id": "tenant", "user_id": "user"}
        | updates
    )


def write(key, content, revision=0):
    return {
        "key": key,
        "expected_revision": revision,
        "kind": "procedure",
        "content": content,
        "evidence": "The user said so",
        "retention_seconds": 86400,
    }


RECORDS = {
    "billing-format": "Billing statement format: totals first, then line items",
    "weather-note": "Check the rain forecast before planning site visits",
    "pasta": "Cooking recipe preference: pasta al dente",
}


def runtime_with(store, embeddings=None, **agent_updates):
    runtime = make_runtime(MockModelProvider(), agent(**agent_updates), learned_memory_store=store)
    runtime.router.register_provider(embeddings or ConceptEmbeddings())
    return runtime


async def remember(runtime, records, **request_updates):
    provider = runtime.router.provider("mock")
    provider.queue(
        *(tool_call_response(MEMORY_WRITE, write(key, text)) for key, text in records.items()),
        "Saved",
    )
    result = await runtime.run(request(**request_updates))
    assert result.status == RunStatus.COMPLETED, result.error


async def context_for(runtime, **updates):
    state = RunState(request=request(**updates))
    await runtime.runs.create(state)
    return ToolContext(
        run_id=state.id,
        step_id="tool-recall",
        tenant_id=state.request.tenant_id,
        user_id=state.request.user_id,
        permissions={"memory.read", "memory.write", "memory.delete"},
        idempotency_key=f"{state.id}:recall",
    )


async def recall(runtime, arguments, **updates):
    result = await runtime.tool_executor.execute(
        MEMORY_RECALL, arguments, await context_for(runtime, **updates)
    )
    return result.value


def keys(value):
    return [record["key"] for record in value["records"]]


async def test_paraphrased_query_finds_a_record_substring_recall_misses():
    store = InMemoryLearnedMemoryStore()
    runtime = runtime_with(store, learned_memory_policy=policy(min_score=0.5))
    await remember(runtime, RECORDS)
    value = await recall(runtime, {"query": "invoice layout"})
    assert keys(value) == ["billing-format"]
    assert value["records"][0]["content"] == RECORDS["billing-format"]
    substring = runtime_with(store, learned_memory_policy=policy(recall="substring"))
    assert keys(await recall(substring, {"query": "invoice layout"})) == []


async def test_unrelated_query_below_min_score_returns_nothing():
    store = InMemoryLearnedMemoryStore()
    runtime = runtime_with(store, learned_memory_policy=policy(min_score=0.5))
    await remember(runtime, RECORDS)
    value = await recall(runtime, {"query": "quarterly headcount plan"})
    assert value["records"] == [] and value["truncated"] is False


async def test_results_are_ranked_by_similarity():
    store = InMemoryLearnedMemoryStore()
    runtime = runtime_with(store)
    await remember(runtime, RECORDS)
    value = await recall(runtime, {"query": "pasta recipe", "limit": 3})
    assert keys(value)[0] == "pasta"


async def test_hybrid_adds_key_and_substring_matches():
    store = InMemoryLearnedMemoryStore()
    runtime = runtime_with(store, learned_memory_policy=policy(recall="hybrid", min_score=0.25))
    await remember(runtime, RECORDS)
    # "site" matches the weather note literally but has no concept in common with it.
    assert keys(await recall(runtime, {"query": "site"})) == ["weather-note"]
    # Similarity alone still ranks a paraphrase.
    assert keys(await recall(runtime, {"query": "invoice layout"})) == ["billing-format"]
    semantic = runtime_with(store, learned_memory_policy=policy(min_score=0.25))
    assert keys(await recall(semantic, {"query": "site"})) == []


async def test_recall_stays_within_max_recall_tokens():
    store = InMemoryLearnedMemoryStore()
    runtime = runtime_with(store, learned_memory_policy=policy(max_recall_tokens=512))
    long = "Billing statement format " + "with detailed invoice line items " * 17
    await remember(runtime, {f"billing-{n}": long for n in range(3)})
    value = await recall(runtime, {"query": "invoice layout", "limit": 3})
    assert value["truncated"] is True
    assert 0 < len(value["records"]) < 3
    message = Message(
        role=Role.TOOL,
        name=MEMORY_RECALL,
        tool_call_id="recall",
        content=(TextBlock(text=json.dumps(value)),),
    )
    assert estimate_message(message) <= 512


@pytest.mark.parametrize(
    "update", [{"tenant_id": "other"}, {"user_id": "other"}, {"agent": "other-agent"}]
)
async def test_similarity_recall_is_scoped_to_tenant_user_and_agent(update):
    store = InMemoryLearnedMemoryStore()
    runtime = runtime_with(store, learned_memory_policy=policy(min_score=0.5))
    runtime.agents.register(agent(name="other-agent"))
    await remember(runtime, RECORDS)
    assert keys(await recall(runtime, {"query": "invoice layout"})) == ["billing-format"]
    assert (await recall(runtime, {"query": "invoice layout"}, **update))["records"] == []
    other = MemoryScope(
        tenant_id=update.get("tenant_id", "tenant"),
        user_id=update.get("user_id", "user"),
        agent_id=update.get("agent", "test-agent"),
    )
    assert await store.unembedded(other, "anything", 21) == ()


async def test_writes_embed_content_and_corrections_replace_the_vector():
    store = InMemoryLearnedMemoryStore()
    embeddings = ConceptEmbeddings()
    runtime = runtime_with(store, embeddings)
    await remember(runtime, {"billing-format": RECORDS["billing-format"]})
    assert embeddings.calls == [([RECORDS["billing-format"]], "concepts")]
    provider = runtime.router.provider("mock")
    provider.queue(
        tool_call_response(MEMORY_WRITE, write("billing-format", "Pasta recipe", revision=1)),
        "Saved",
    )
    assert (await runtime.run(request())).status == RunStatus.COMPLETED
    assert keys(await recall(runtime, {"query": "cooking"})) == ["billing-format"]
    assert keys(await recall(runtime, {"query": "invoice"})) == ["billing-format"]
    # The ranking uses the corrected content only: the old vector is gone.
    ranked = await store.rank(
        MemoryScope(tenant_id="tenant", user_id="user", agent_id="test-agent"),
        MemoryEmbedding.normalized(policy().embedding_identity, [1, 0, 0, 0, 0, 0, 0, 0]),
        21,
    )
    assert [score for _, score in ranked] == [0.0]


async def test_deletion_and_expiry_remove_records_and_vectors():
    store = InMemoryLearnedMemoryStore()
    runtime = runtime_with(store)
    await remember(runtime, RECORDS)
    provider = runtime.router.provider("mock")
    provider.queue(
        tool_call_response(MEMORY_DELETE, {"key": "billing-format", "expected_revision": 1}),
        "Deleted",
    )
    assert (await runtime.run(request())).status == RunStatus.COMPLETED
    assert "billing-format" not in keys(await recall(runtime, {"query": "invoice layout"}))
    key = ("tenant", "user", "test-agent", "pasta")
    assert key in store._vectors
    store._records[key] = store._records[key].model_copy(
        update={"expires_at": utc_now() - timedelta(seconds=1)}
    )
    assert "pasta" not in keys(await recall(runtime, {"query": "pasta recipe"}))
    assert await store.purge_expired() == 1
    assert key not in store._vectors
    assert ("tenant", "user", "test-agent", "billing-format") not in store._vectors


async def test_records_from_substring_mode_and_a_previous_model_are_reembedded():
    store = InMemoryLearnedMemoryStore()
    plain = runtime_with(store, learned_memory_policy=policy(recall="substring"))
    embeddings = ConceptEmbeddings()
    await remember(plain, RECORDS)
    assert store._vectors == {}
    semantic = runtime_with(store, embeddings)
    assert keys(await recall(semantic, {"query": "invoice layout"}))[0] == "billing-format"
    # One provider call embedded the query and the three records written without vectors.
    assert len(embeddings.calls) == 1 and len(embeddings.calls[0][0]) == 4
    await recall(semantic, {"query": "invoice layout"})
    assert len(embeddings.calls[1][0]) == 1
    upgraded = runtime_with(store, embeddings, learned_memory_policy=policy(embedding_version="2"))
    assert keys(await recall(upgraded, {"query": "invoice layout"}))[0] == "billing-format"
    assert len(embeddings.calls[2][0]) == 4
    identity = policy(embedding_version="2").embedding_identity
    assert {vector.model for _, vector in store._vectors.values()} == {identity}


async def test_key_and_empty_query_recall_do_not_embed():
    store = InMemoryLearnedMemoryStore()
    embeddings = ConceptEmbeddings()
    runtime = runtime_with(store, embeddings)
    await remember(runtime, RECORDS)
    calls = len(embeddings.calls)
    assert keys(await recall(runtime, {"key": "pasta"})) == ["pasta"]
    assert len(keys(await recall(runtime, {"query": ""}))) == 3
    assert len(embeddings.calls) == calls


async def test_embedding_failure_does_not_reveal_the_query():
    store = InMemoryLearnedMemoryStore()
    runtime = runtime_with(store, ConceptEmbeddings(fail=True))
    context = await context_for(runtime)
    with pytest.raises(ToolExecutionError) as error:
        await runtime.tools.get(MEMORY_RECALL).execute({"query": "secret invoice"}, context)
    assert "secret" not in str(error.value) and "RuntimeError" in str(error.value)


def test_similarity_recall_requires_an_embedding_provider():
    with pytest.raises(ValidationError, match="embedding_provider"):
        LearnedMemoryPolicy(enabled=True, recall="semantic")
    assert LearnedMemoryPolicy().recall == "substring"
    assert LearnedMemoryPolicy().embedding_identity is None


async def test_unregistered_provider_or_vectorless_store_is_a_configuration_error():
    runtime = make_runtime(
        MockModelProvider(), agent(), learned_memory_store=InMemoryLearnedMemoryStore()
    )
    with pytest.raises(ConfigurationError, match="embedding_provider"):
        await runtime.run(request())

    class PlainStore:
        def __init__(self) -> None:
            self.inner = InMemoryLearnedMemoryStore()

        async def get(self, scope, key):
            return await self.inner.get(scope, key)

        async def search(self, scope, query, limit):
            return await self.inner.search(scope, query, limit)

        async def apply(self, mutation):
            return await self.inner.apply(mutation)

        async def purge_expired(self, limit=1000):
            return await self.inner.purge_expired(limit)

    plain = runtime_with(PlainStore())
    with pytest.raises(ConfigurationError, match="keeps vectors"):
        plain.learned_memory.check_policy(policy())


def _settings(embedding_provider: str) -> AppSettings:
    return AppSettings.model_validate(
        {
            "providers": {"local": {"type": "mock", "default_model": "m"}},
            "agents": [
                {
                    "name": "operator",
                    "version": "1.0.0",
                    "description": "d",
                    "system_instructions": "x",
                    "default_model": {"name": "m", "provider": "local"},
                    "learned_memory_policy": {
                        "enabled": True,
                        "recall": "hybrid",
                        "embedding_provider": embedding_provider,
                    },
                }
            ],
        }
    )


def test_settings_reject_an_unknown_memory_embedding_provider():
    assert _settings("local").agents[0].learned_memory_policy.recall == "hybrid"
    with pytest.raises(ValidationError, match=r"learned_memory_policy\.embedding_provider"):
        _settings("missing")

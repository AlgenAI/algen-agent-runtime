from __future__ import annotations

import pytest
from conftest import make_agent

from algen_agent_runtime.context.builder import DefaultContextBuilder, RetrievalContextBuilder
from algen_agent_runtime.exceptions.errors import BudgetExceededError, ConflictError
from algen_agent_runtime.persistence.memory import InMemoryMemoryStore
from algen_agent_runtime.retrieval.contracts import RetrievalQuery, RetrievedDocument
from algen_agent_runtime.types.contracts import Message, Role, RunRequest, RunState, ToolCall


class StaticRetriever:
    def __init__(self, documents: tuple[RetrievedDocument, ...] = ()) -> None:
        self.documents = documents

    async def retrieve(self, query: RetrievalQuery) -> tuple[RetrievedDocument, ...]:
        return self.documents


def state() -> RunState:
    return RunState(
        request=RunRequest(
            agent="test-agent", input="current task", tenant_id="tenant", user_id="u"
        ),
        session_id="session",
    )


def tool_exchange() -> list[Message]:
    return [
        Message(
            role=Role.ASSISTANT,
            content=(),
            tool_calls=(ToolCall(id="a", name="lookup"), ToolCall(id="b", name="lookup")),
        ),
        Message(role=Role.TOOL, content=(), tool_call_id="a"),
        Message(role=Role.TOOL, content=(), tool_call_id="b"),
    ]


@pytest.mark.parametrize("retrieval", [False, True])
async def test_repeated_builds_and_checkpoint_restore_do_not_duplicate_history(
    retrieval: bool,
) -> None:
    memory = InMemoryMemoryStore()
    history = [Message.text(Role.USER, "earlier"), Message.text(Role.ASSISTANT, "previous answer")]
    await memory.append("tenant", "session", history)
    builder = (
        RetrievalContextBuilder("rag", memory, StaticRetriever())
        if retrieval
        else DefaultContextBuilder(memory)
    )
    current = state()
    first = list(await builder.build(current, make_agent()))
    current.messages = list(first)
    for message in tool_exchange():
        current.append_message(message)
    restored = RunState.model_validate_json(current.model_dump_json())

    second = list(await builder.build(restored, make_agent()))
    restored.messages = second
    third = list(await builder.build(restored, make_agent()))

    assert second == first + tool_exchange()
    assert third == second
    assert [message for message in second if message.role == Role.SYSTEM] == [first[0]]


@pytest.mark.parametrize("policy", [{"enabled": False}, {"max_items": 0}])
async def test_disabled_memory_is_not_loaded(policy: dict[str, object]) -> None:
    memory = InMemoryMemoryStore()
    await memory.append("tenant", "session", [Message.text(Role.USER, "private history")])
    messages = await DefaultContextBuilder(memory).build(state(), make_agent(memory_policy=policy))
    assert [message.text_content for message in messages] == ["Be deterministic.", "current task"]


def test_pack_preserves_instructions_current_request_and_recent_complete_exchange() -> None:
    system = Message.text(Role.SYSTEM, "Required instructions")
    task = Message.text(Role.USER, "current task")
    recent = tool_exchange()
    required = [system, task, *recent]
    budget = sum(DefaultContextBuilder.estimate(message) for message in required)
    builder = DefaultContextBuilder(InMemoryMemoryStore(), budget)
    messages = [system, Message.text(Role.USER, "old request"), task, *tool_exchange(), *recent]

    assert builder.pack(messages) == tuple(required)
    assert sum(builder.estimate(message) for message in builder.pack(messages)) <= budget


def test_pack_evicts_a_whole_tool_exchange_when_only_results_would_fit() -> None:
    system = Message.text(Role.SYSTEM, "instructions")
    task = Message.text(Role.USER, "task")
    last = Message.text(Role.ASSISTANT, "latest response")
    required = [system, task, last]
    budget = sum(DefaultContextBuilder.estimate(message) for message in required) + 50
    builder = DefaultContextBuilder(InMemoryMemoryStore(), budget)

    assert builder.pack([system, task, *tool_exchange(), last]) == tuple(required)


def test_pack_preserves_partial_parallel_tool_exchange_between_tool_executions() -> None:
    messages = [Message.text(Role.USER, "task"), *tool_exchange()[:2]]
    assert DefaultContextBuilder(InMemoryMemoryStore()).pack(messages) == tuple(messages)


@pytest.mark.parametrize("role", [Role.SYSTEM, Role.USER])
def test_oversized_required_message_fails_without_truncation(role: Role) -> None:
    builder = DefaultContextBuilder(InMemoryMemoryStore(), 100)
    with pytest.raises(BudgetExceededError, match="required context exceeds"):
        builder.pack([Message.text(role, "x" * 1000)])


def test_tool_arguments_count_toward_context_budget() -> None:
    call = Message(
        role=Role.ASSISTANT,
        content=(),
        tool_calls=(ToolCall(id="a", name="lookup", arguments={"query": "x" * 1000}),),
    )
    with pytest.raises(BudgetExceededError):
        DefaultContextBuilder(InMemoryMemoryStore(), 100).pack([call])


def test_orphan_tool_result_is_rejected() -> None:
    with pytest.raises(ConflictError, match="without its tool call"):
        DefaultContextBuilder(InMemoryMemoryStore()).pack(tool_exchange()[1:])


async def test_retrieval_without_results_still_enforces_context_limit() -> None:
    builder = RetrievalContextBuilder(
        "rag", InMemoryMemoryStore(), StaticRetriever(), max_context_tokens=100
    )
    current = state()
    current.request = current.request.model_copy(update={"input": "x" * 1000})
    with pytest.raises(BudgetExceededError):
        await builder.build(current, make_agent())


@pytest.mark.parametrize("retrieval_budget,context_budget", [(10, 1000), (1000, 100)])
async def test_oversized_evidence_is_omitted_without_stale_citations(
    retrieval_budget: int, context_budget: int
) -> None:
    document = RetrievedDocument(id="large", text="evidence " * 100, source="kb://large", score=1)
    builder = RetrievalContextBuilder(
        "rag",
        InMemoryMemoryStore(),
        StaticRetriever((document,)),
        max_retrieval_tokens=retrieval_budget,
        max_context_tokens=context_budget,
    )
    current = state()
    messages = await builder.build(current, make_agent())
    assert len(messages) == 2
    assert not current.citations


async def test_repeated_retrieval_refreshes_evidence_and_preserves_history_once() -> None:
    memory = InMemoryMemoryStore()
    await memory.append("tenant", "session", [Message.text(Role.USER, "earlier")])
    retriever = StaticRetriever(
        (RetrievedDocument(id="first", text="first evidence", source="kb://first", score=1),)
    )
    builder = RetrievalContextBuilder("rag", memory, retriever)
    current = state()
    current.messages = list(await builder.build(current, make_agent()))
    retriever.documents = (
        RetrievedDocument(id="second", text="replacement evidence", source="kb://second", score=1),
    )
    messages = await builder.build(current, make_agent())
    assert [message.text_content for message in messages].count("earlier") == 1
    assert sum(message.role == Role.SYSTEM for message in messages) == 2
    assert "first evidence" not in "".join(message.text_content for message in messages)
    assert [citation.document_id for citation in current.citations] == ["second"]


@pytest.mark.parametrize("limit", [0, -1])
def test_context_limit_must_be_positive(limit: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        DefaultContextBuilder(InMemoryMemoryStore(), limit)

from __future__ import annotations

import pytest


def test_langgraph_sdk_surface() -> None:
    graph = pytest.importorskip("langgraph.graph")
    assert hasattr(graph.StateGraph, "compile")


def test_openai_agents_sdk_surface() -> None:
    agents = pytest.importorskip("agents")
    assert hasattr(agents.Runner, "run")
    assert hasattr(agents.Runner, "run_streamed")


def test_autogen_agentchat_sdk_surface() -> None:
    base = pytest.importorskip("autogen_agentchat.base")
    assert hasattr(base.ChatAgent, "run")
    assert hasattr(base.ChatAgent, "run_stream")


def test_crewai_sdk_surface() -> None:
    crewai = pytest.importorskip("crewai")
    assert hasattr(crewai.Crew, "kickoff")
    assert hasattr(crewai.Crew, "kickoff_async")

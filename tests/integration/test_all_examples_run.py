"""Every runnable example under examples/ runs to completion, offline and non-interactively.

A module is runnable when it has an ``if __name__ == "__main__":`` block. Each one must be in
``RUNS`` (with the arguments that make it offline and non-interactive) or in ``SKIPPED`` (with
the external service it needs), so a new example cannot be added without a smoke run, and an
example cannot silently rot. The runs are subprocesses with no provider keys in their
environment, started together on a small thread pool, each with its own timeout.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
TIMEOUT_SECONDS = 60
WORKERS = min(8, os.cpu_count() or 2)

# Module (relative to ``examples``) -> the argument lists to run it with, one run per list.
RUNS: dict[str, tuple[tuple[str, ...], ...]] = {
    "context_compaction": ((),),
    "context_evaluation": ((),),
    "context_offloading": ((),),
    "learned_memory": ((),),
    "progressive_skills": ((),),
    "working_state": ((),),
    "quickstart_tool.app": ((),),
    "quickstart_agent_tool.app": ((), ("--reject",)),
    "quickstart_approval.app": (("--approve-all",), ("--reject-all",)),
    "quickstart_cloud_chat.app": (("--offline",),),
    "quickstart_conversation.app": ((), ("--reject",)),
    "quickstart_rag.app": (("--offline",), ("--offline", "--provider", "mistral")),
    "pattern_approval_workflow.app": (("--approve-all",),),
    "pattern_child_workflow.app": ((),),
    "pattern_evaluation_gate.app": ((),),
    "pattern_governed_research.app": ((),),
    "pattern_guarded_tools.app": ((),),
    "pattern_langgraph_governance.app": ((),),
    "pattern_mcp_governance.app": ((),),
    "pattern_multi_agent_fanout.app": ((),),
    "pattern_openai_agents_integration.app": ((),),
    "pattern_provider_fallback.app": (("--offline",),),
    "pattern_secured_api.app": ((),),
    "pattern_side_effect_reconciliation.app": ((),),
    "pattern_text_to_sql.app": (("--offline",),),
    "reference_customer_support.app": (
        ("--answers-file", "examples/reference_customer_support/fixtures/automation-answers.json"),
    ),
    "reference_incident_response.app": (("--approve-all",),),
    "reference_invoice_exceptions.app": (("--approve-all",),),
    "case_study_teaching_assistant.app": (("--offline",),),
}

# Module -> why it is not run here. Each needs a service CI does not provide, or is not a script.
SKIPPED: dict[str, str] = {
    "quickstart_local_chat.app": "needs a local Ollama server",
    "pattern_text_to_sql.advanced.app": "needs pgvector (PGVECTOR_DSN) and OpenAI embeddings",
    "case_study_responsible_hiring.app": "needs pgvector (PGVECTOR_DSN)",
    "case_study_responsible_hiring.dashboard": "a long-running HTTP server that needs pgvector",
    "case_study_teaching_assistant.dashboard": "a long-running HTTP server",
    "pattern_mcp_governance.server": "the stdio MCP server that pattern_mcp_governance.app starts",
}

# Module -> the optional package it needs; without it (the base CI job) the run is skipped.
REQUIRES: dict[str, str] = {
    "pattern_mcp_governance.app": "mcp",
    "pattern_openai_agents_integration.app": "agents",
}

# Never let a developer's keys turn a smoke run into a paid, networked one.
SCRUBBED_ENVIRONMENT = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "MISTRAL_API_KEY",
    "GEMINI_API_KEY",
    "XAI_API_KEY",
    "GROQ_API_KEY",
    "TRACCIA_API_KEY",
)

CASES = [
    pytest.param(module, arguments, id=" ".join((module, *arguments)))
    for module, runs in RUNS.items()
    for arguments in runs
]


def runnable_modules() -> set[str]:
    found = set()
    for path in EXAMPLES.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if 'if __name__ == "__main__":' in path.read_text(encoding="utf-8"):
            found.add(".".join(path.relative_to(EXAMPLES).with_suffix("").parts))
    return found


def _run(module: str, arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in SCRUBBED_ENVIRONMENT and not key.startswith("ALGEN_AGENT_RUNTIME__")
    }
    return subprocess.run(
        [sys.executable, "-m", f"examples.{module}", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
        check=False,
    )


@pytest.fixture(scope="module")
def example_runs(
    request: pytest.FixtureRequest,
) -> Iterator[dict[tuple[str, tuple[str, ...]], Future[subprocess.CompletedProcess[str]]]]:
    """Start every selected run at once, so the module takes about as long as its slowest few."""
    selected = []
    for item in request.session.items:
        callspec = getattr(item, "callspec", None)
        if callspec is None or getattr(item, "originalname", None) != "test_example_runs":
            continue
        module, arguments = callspec.params["module"], callspec.params["arguments"]
        if REQUIRES.get(module) is None or _installed(REQUIRES[module]):
            selected.append((module, arguments))
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        yield {case: pool.submit(_run, *case) for case in selected}


def _installed(package: str) -> bool:
    try:
        __import__(package)
    except ImportError:
        return False
    return True


def test_every_runnable_example_is_run_or_skipped_with_a_reason() -> None:
    found = runnable_modules()
    listed = set(RUNS) | set(SKIPPED)

    assert not set(RUNS) & set(SKIPPED)
    assert found - listed == set(), (
        "New runnable examples: add each to RUNS with offline, non-interactive arguments "
        "(or to SKIPPED with the service it needs)"
    )
    assert listed - found == set(), "Listed examples that no longer exist or are not runnable"
    assert set(REQUIRES) <= set(RUNS)


@pytest.mark.parametrize(("module", "arguments"), CASES)
def test_example_runs(
    module: str,
    arguments: tuple[str, ...],
    example_runs: dict[tuple[str, tuple[str, ...]], Future[subprocess.CompletedProcess[str]]],
) -> None:
    if module in REQUIRES:
        pytest.importorskip(REQUIRES[module])
    result = example_runs[(module, arguments)].result()

    assert result.returncode == 0, (
        f"python -m examples.{module} {' '.join(arguments)} exited {result.returncode}\n"
        f"STDOUT:\n{result.stdout[-4000:]}\nSTDERR:\n{result.stderr[-4000:]}"
    )
    assert result.stdout.strip(), "an example should print what it did"

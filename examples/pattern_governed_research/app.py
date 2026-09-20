from __future__ import annotations

# TODO: Implement this entry point once retrieval/HTTP tools are registered.
#
# The research agent requires at least one tool (e.g. research.fetch or research.search)
# to be registered with build_container before it can perform evidence retrieval.
# See agent.yaml for the full TODO checklist.
#
# Placeholder structure (not yet runnable):
#
#   from algen_agent_runtime.config.settings import load_settings
#   from algen_agent_runtime.orchestration.container import build_container
#   from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
#   from algen_agent_runtime.types.contracts import RunRequest
#
#   async def research(query: str) -> str:
#       settings = load_settings((Path(__file__).parent / "agent.yaml",))
#       container = build_container(settings)
#       # container.tools.register(create_http_tool())  # <-- register tools here
#       try:
#           result = await AlgenAgentRuntimeClient(container.runtime).run(
#               RunRequest(agent="pattern-governed-research", input=query, ...)
#           )
#           return result.output or ""
#       finally:
#           container.close()


def main() -> None:
    raise NotImplementedError(
        "pattern_governed_research is not yet runnable. "
        "See agent.yaml and README.md for the implementation checklist."
    )


if __name__ == "__main__":
    main()

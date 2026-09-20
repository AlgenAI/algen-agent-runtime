from __future__ import annotations

# TODO: Implement this entry point once domain tools and native workflow execution are available.
#
# The workflow agent requires:
#   1. CRM domain tools (crm.lookup, crm.update) registered via container.tools.register().
#   2. Native runtime workflow execution of the AgentDefinition.workflow graph
#      (or an explicit workflow.py implementation using a workflow SDK).
#
# See agent.yaml and README.md for the full implementation checklist.
#
# Placeholder structure (not yet runnable):
#
#   async def run_workflow(case_id: str) -> str:
#       settings = load_settings((Path(__file__).parent / "agent.yaml",))
#       container = build_container(settings)
#       # container.tools.register(create_crm_lookup_tool())
#       # container.tools.register(create_crm_update_tool())
#       try:
#           result = await AlgenAgentRuntimeClient(container.runtime).run(
#               RunRequest(agent="pattern-approval-workflow", input=case_id, ...)
#           )
#           return result.output or ""
#       finally:
#           container.close()


def main() -> None:
    raise NotImplementedError(
        "pattern_approval_workflow is not yet runnable. "
        "See agent.yaml and README.md for the implementation checklist."
    )


if __name__ == "__main__":
    main()

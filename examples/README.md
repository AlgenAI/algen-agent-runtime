# Examples

Run every command from the repository root after `pip install -e '.[dev]'`. "Offline" means no
API key and no network: the deterministic mock provider stands in for the model. Hosted-provider
examples take `--offline` to run that way.

## Quickstarts

| Example | Shows | Offline? | Command |
| --- | --- | --- | --- |
| [`quickstart_approval`](quickstart_approval) | A proposal, a first-class approval node, approve/modify/reject, idempotent apply | Yes | `python -m examples.quickstart_approval.app` |
| [`quickstart_tool`](quickstart_tool) | A workflow backed by a deterministic, read-only tool | Yes | `python -m examples.quickstart_tool.app` |
| [`quickstart_agent_tool`](quickstart_agent_tool) | Typed function tools grouped in a `Toolkit` and declared through a plugin | Yes | `python -m examples.quickstart_agent_tool.app` |
| [`quickstart_conversation`](quickstart_conversation) | A two-turn conversation through `ConversationService` | Yes | `python -m examples.quickstart_conversation.app` |
| [`quickstart_cloud_chat`](quickstart_cloud_chat) | One hosted-provider agent with a model allowlist, guardrails and verification | With `--offline` | `python -m examples.quickstart_cloud_chat.app --offline` |
| [`quickstart_rag`](quickstart_rag) | Retrieval-augmented answers with citations | With `--offline` | `python -m examples.quickstart_rag.app --offline` |
| [`quickstart_local_chat`](quickstart_local_chat) | A local model through Ollama | Needs a local Ollama server | `python -m examples.quickstart_local_chat.app` |

## Patterns

| Example | Shows | Offline? | Command |
| --- | --- | --- | --- |
| [`pattern_approval_workflow`](pattern_approval_workflow) | A CRM lookup, a structured proposal and an approval pause in a workflow DAG | Yes | `python -m examples.pattern_approval_workflow.app --approve-all` |
| [`pattern_child_workflow`](pattern_child_workflow) | A `workflow` node dispatching a pinned child workflow | Yes | `python -m examples.pattern_child_workflow.app` |
| [`pattern_evaluation_gate`](pattern_evaluation_gate) | Golden questions through `EvaluationRunner` as a release gate | Yes | `python -m examples.pattern_evaluation_gate.app` |
| [`pattern_governed_research`](pattern_governed_research) | A credential-free evidence workflow with citations | Yes | `python -m examples.pattern_governed_research.app` |
| [`pattern_guarded_tools`](pattern_guarded_tools) | The controls the Runtime applies around tool and model calls | Yes | `python -m examples.pattern_guarded_tools.app` |
| [`pattern_multi_agent_fanout`](pattern_multi_agent_fanout) | A Runtime-owned fan-out and join across agents | Yes | `python -m examples.pattern_multi_agent_fanout.app` |
| [`pattern_provider_fallback`](pattern_provider_fallback) | Falling back across providers | With `--offline` | `python -m examples.pattern_provider_fallback.app --offline` |
| [`pattern_secured_api`](pattern_secured_api) | The HTTP API with OIDC authentication, served in-process | Yes | `python -m examples.pattern_secured_api.app` |
| [`pattern_side_effect_reconciliation`](pattern_side_effect_reconciliation) | Reconciling a write whose outcome is unknown | Yes | `python -m examples.pattern_side_effect_reconciliation.app` |
| [`pattern_text_to_sql`](pattern_text_to_sql) | Analytics questions to SQLite SQL through a read-only tool | With `--offline` | `python -m examples.pattern_text_to_sql.app --offline` |
| [`pattern_langgraph_governance`](pattern_langgraph_governance) | `LangGraphAdapter`: identity, status and streaming for a graph | Yes | `python -m examples.pattern_langgraph_governance.app` |
| [`pattern_openai_agents_integration`](pattern_openai_agents_integration) | `OpenAIAgentsAdapter`, and Runtime tools governed inside the SDK | Yes, with the `openai-agents` extra | `python -m examples.pattern_openai_agents_integration.app` |
| [`pattern_mcp_governance`](pattern_mcp_governance) | MCP servers declared in YAML, with operator policy overrides | Yes, with the `mcp` extra | `python -m examples.pattern_mcp_governance.app` |

`pattern_text_to_sql/advanced` needs pgvector (`PGVECTOR_DSN`) and OpenAI embeddings; see its
README.

## Reference applications

| Example | Shows | Offline? | Command |
| --- | --- | --- | --- |
| [`reference_customer_support`](reference_customer_support) | A Studio-importable support workflow | Yes | `python -m examples.reference_customer_support.app --answers-file examples/reference_customer_support/fixtures/automation-answers.json` |
| [`reference_incident_response`](reference_incident_response) | A multi-agent incident workflow with approvals | Yes | `python -m examples.reference_incident_response.app --approve-all` |
| [`reference_invoice_exceptions`](reference_invoice_exceptions) | An accounts-payable exception workflow | Yes | `python -m examples.reference_invoice_exceptions.app --approve-all` |

## Case studies

These stay in the repository and are not installed with the package.

| Example | Shows | Offline? | Command |
| --- | --- | --- | --- |
| [`case_study_teaching_assistant`](case_study_teaching_assistant) | A grounded teaching assistant traced with Traccia | With `--offline` | `python -m examples.case_study_teaching_assistant.app --offline` |
| [`case_study_responsible_hiring`](case_study_responsible_hiring) | A tenant-isolated hiring review with resume inspection and human decisions | Needs pgvector and an OpenAI key | `python -m examples.case_study_responsible_hiring.app` |

## Single-file examples

| Example | Shows | Offline? | Command |
| --- | --- | --- | --- |
| [`context_compaction.py`](context_compaction.py) | History compaction with a deterministic summary | Yes | `python -m examples.context_compaction` |
| [`context_evaluation.py`](context_evaluation.py) | An offline context regression gate | Yes | `python -m examples.context_evaluation` |
| [`context_offloading.py`](context_offloading.py) | Recoverable, governed paging of large tool results | Yes | `python -m examples.context_offloading` |
| [`learned_memory.py`](learned_memory.py) | Remembering a preference and recalling it in another run | Yes | `python -m examples.learned_memory` |
| [`progressive_skills.py`](progressive_skills.py) | Loading selected procedural guidance from a skill catalog | Yes | `python -m examples.progressive_skills` |
| [`working_state.py`](working_state.py) | Structured task progress kept in a run checkpoint | Yes | `python -m examples.working_state` |

`plugins.py` is a minimal plugin module (`examples.plugins:UtilitiesPlugin`, one
`text.word_count` tool) to copy when writing a plugin; see [plugins](../docs/plugins.md).
`offline.py` and `workflow_cli.py` are helpers the examples share.
`tests/integration/test_all_examples_run.py` runs every offline command above.

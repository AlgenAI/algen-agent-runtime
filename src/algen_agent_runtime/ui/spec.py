"""Specification extractor for Agent Manifest UI."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import yaml

from algen_agent_runtime import __version__
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.orchestration.container import Container
from algen_agent_runtime.types.contracts import AgentDefinition
from algen_agent_runtime.workflows.contracts import WorkflowManifest, WorkflowNode


def _compute_dag_layout(nodes: Sequence[WorkflowNode]) -> dict[str, Any]:
    """Compute DAG layout levels and edges for visual rendering."""
    node_map = {n.id: n for n in nodes}
    in_degrees: dict[str, int] = {n.id: 0 for n in nodes}
    dependents: dict[str, list[str]] = {n.id: [] for n in nodes}

    for n in nodes:
        for dep in n.depends_on:
            if dep in dependents:
                dependents[dep].append(n.id)
            if dep in node_map:
                in_degrees[n.id] += 1

    # Topological level calculation
    levels: dict[str, int] = {}
    current_level = 0
    # Start with nodes that have no dependencies in this workflow
    queue = [n.id for n in nodes if in_degrees[n.id] == 0]
    visited = set()

    for item in queue:
        levels[item] = 0

    while queue:
        next_queue: list[str] = []
        for node_id in queue:
            visited.add(node_id)
            node_level = levels.get(node_id, current_level)
            for child_id in dependents.get(node_id, []):
                levels[child_id] = max(levels.get(child_id, 0), node_level + 1)
                in_degrees[child_id] -= 1
                if in_degrees[child_id] <= 0 and child_id not in visited:
                    next_queue.append(child_id)
        queue = next_queue
        current_level += 1

    # Handle any isolated or circular nodes gracefully
    for n in nodes:
        if n.id not in levels:
            levels[n.id] = current_level

    # Group nodes by level
    max_level = max(levels.values()) if levels else 0
    columns: list[list[str]] = [[] for _ in range(max_level + 1)]
    for node_id, lvl in levels.items():
        columns[lvl].append(node_id)

    # Collect edges
    edges: list[dict[str, str]] = []
    for n in nodes:
        for dep in n.depends_on:
            edges.append({"source": dep, "target": n.id})

    return {
        "levels": levels,
        "columns": columns,
        "edges": edges,
        "max_level": max_level,
    }


def _serialize_agent(agent: AgentDefinition) -> dict[str, Any]:
    """Serialize an AgentDefinition for the UI."""
    return {
        "name": agent.name,
        "version": agent.version,
        "logical_id": agent.logical_id,
        "key": agent.key,
        "description": agent.description,
        "system_instructions": agent.system_instructions,
        "planning_strategy": agent.planning_strategy,
        "max_steps": agent.max_steps,
        "context_builder": agent.context_builder,
        "response_composer": agent.response_composer,
        "default_model": {
            "name": agent.default_model.name,
            "provider": agent.default_model.provider,
            "model": agent.default_model.model,
            "required_capabilities": sorted(list(agent.default_model.required_capabilities)),
            "quality_tier": agent.default_model.quality_tier,
            "routing_preference": agent.default_model.routing_preference,
        },
        "fallback_models": [
            {
                "name": fb.name,
                "provider": fb.provider,
                "model": fb.model,
                "required_capabilities": sorted(list(fb.required_capabilities)),
            }
            for fb in agent.fallback_models
        ],
        "model_allowlist": sorted(list(agent.model_allowlist)),
        "capabilities": sorted(list(agent.capabilities)),
        "enabled_tools": sorted(list(agent.enabled_tools)),
        "tool_permissions": sorted(list(agent.tool_permissions)),
        "memory_policy": agent.memory_policy.model_dump(mode="json"),
        "guardrail_policy": agent.guardrail_policy.model_dump(mode="json"),
        "verification_policy": agent.verification_policy.model_dump(mode="json"),
        "retry_policy": agent.retry_policy.model_dump(mode="json"),
        "approval_policy": agent.approval_policy.model_dump(mode="json"),
        "budget": agent.budget.model_dump(mode="json"),
        "prompt_templates": dict(agent.prompt_templates),
        "metadata": dict(agent.metadata),
        "tags": sorted(list(agent.tags)),
    }


def _serialize_workflow(workflow: WorkflowManifest) -> dict[str, Any]:
    """Serialize a WorkflowManifest for the UI, including DAG visualization layout."""
    dag_layout = _compute_dag_layout(workflow.nodes)

    serialized_nodes: list[dict[str, Any]] = []
    for node in workflow.nodes:
        serialized_nodes.append(
            {
                "id": node.id,
                "kind": str(node.kind.value if hasattr(node.kind, "value") else node.kind),
                "depends_on": list(node.depends_on),
                "output_key": node.output_key,
                "agent": node.agent,
                "handler": node.handler,
                "workflow_name": node.workflow_name,
                "workflow_version": node.workflow_version,
                "input_builder": node.input_builder,
                "input_template": node.input_template,
                "metadata_builder": node.metadata_builder,
                "validator": node.validator,
                "output_schema": node.output_schema,
                "output_schema_hook": node.output_schema_hook,
                "max_repairs": node.max_repairs,
                "map_from": node.map_from,
                "max_fan_out": node.max_fan_out,
                "pause": node.pause.model_dump(mode="json") if node.pause else None,
                "approval": node.approval.model_dump(mode="json") if node.approval else None,
                "condition": node.condition.model_dump(mode="json") if node.condition else None,
                "run_if": node.run_if.model_dump(mode="json") if node.run_if else None,
                "join_strategy": str(node.join_strategy),
                "max_iterations": node.max_iterations,
                "loop_until": node.loop_until.model_dump(mode="json") if node.loop_until else None,
                "failure_policy": str(node.failure_policy),
                "recovery_policy": str(node.recovery_policy),
                "resources": [
                    {
                        "kind": str(r.kind.value if hasattr(r.kind, "value") else r.kind),
                        "name": r.name,
                        "access": str(r.access),
                        "description": r.description,
                    }
                    for r in node.resources
                ],
                "metadata": dict(node.metadata),
            }
        )

    return {
        "name": workflow.name,
        "version": workflow.version,
        "description": workflow.description,
        "maximum_concurrency": workflow.maximum_concurrency,
        "timeout_seconds": workflow.timeout_seconds,
        "output_key": workflow.output_key,
        "hook_provider": workflow.hook_provider,
        "input_schema": workflow.input_schema,
        "output_schema": workflow.output_schema,
        "metadata": dict(workflow.metadata),
        "nodes": serialized_nodes,
        "dag": dag_layout,
    }


def extract_agent_manifest_spec(
    settings: AppSettings,
    container: Container | None = None,
    raw_yaml: str | None = None,
    live_server: bool = False,
    api_base_url: str = "",
    title: str | None = None,
) -> dict[str, Any]:
    """Extract a comprehensive JSON-serializable specification for UI rendering."""
    # Collect agents from settings and container
    agent_map: dict[str, AgentDefinition] = {}
    for agent in settings.agents:
        agent_map[agent.name] = agent
    if container is not None and hasattr(container, "agents"):
        for dynamic_agent in container.agents.list():
            agent_map[dynamic_agent.name] = dynamic_agent

    serialized_agents = [_serialize_agent(a) for a in agent_map.values()]

    # Collect workflows
    serialized_workflows = {
        name: _serialize_workflow(wf) for name, wf in settings.workflows.items()
    }

    # Collect providers
    serialized_providers = []
    for p_id, p in settings.providers.items():
        # Secret reference sanitization (ensure literal secrets are never included)
        secret_ref = (
            p.api_key if p.api_key and p.api_key.startswith(("env://", "secret://")) else None
        )
        serialized_providers.append(
            {
                "id": p_id,
                "type": p.type,
                "base_url": p.base_url,
                "default_model": p.default_model,
                "api_key_ref": secret_ref,
                "has_credentials": bool(secret_ref),
                "capabilities": p.capabilities.model_dump(mode="json"),
                "timeout_seconds": p.timeout_seconds,
                "max_concurrency": p.max_concurrency,
                "requests_per_minute": p.requests_per_minute,
                "cost_per_1k_input": p.cost_per_1k_input,
                "cost_per_1k_output": p.cost_per_1k_output,
            }
        )

    # Collect MCP servers
    serialized_mcp_servers = []
    for s in settings.mcp_servers:
        transport_val = str(s.transport.value if hasattr(s.transport, "value") else s.transport)
        cmd = getattr(s, "command", None)
        args_list = list(getattr(s, "args", []))
        env_dict = getattr(s, "env", {})
        url_val = getattr(s, "url", None)
        serialized_mcp_servers.append(
            {
                "name": s.name,
                "transport": transport_val,
                "command": cmd,
                "args": args_list,
                "env_keys": sorted(list(env_dict.keys())),
                "url": url_val,
            }
        )

    # Collect retrieval stores
    serialized_retrieval = []
    for r_name, r in settings.retrieval.items():
        serialized_retrieval.append(
            {
                "name": r_name,
                "type": r.type,
                "mode": str(r.mode.value if hasattr(r.mode, "value") else r.mode),
                "limit": r.limit,
                "minimum_score": r.minimum_score,
                "keyword_weight": r.keyword_weight,
                "vector_weight": r.vector_weight,
                "embedding_provider": r.embedding_provider,
                "embedding_model": r.embedding_model,
                "embedding_dimensions": r.embedding_dimensions,
                "chunk_size": r.chunk_size,
                "chunk_overlap": r.chunk_overlap,
                "collection_name": r.collection_name,
                "table_name": r.table_name,
            }
        )

    # Collect tools across agents, workflows, and container
    tools_dict: dict[str, dict[str, Any]] = {}
    for agent in agent_map.values():
        for tool_name in agent.enabled_tools:
            if tool_name not in tools_dict:
                tools_dict[tool_name] = {
                    "name": tool_name,
                    "used_by_agents": [agent.name],
                    "used_by_nodes": [],
                    "description": "",
                }
            elif agent.name not in tools_dict[tool_name]["used_by_agents"]:
                tools_dict[tool_name]["used_by_agents"].append(agent.name)

    for wf in settings.workflows.values():
        for node in wf.nodes:
            for res in node.resources:
                if str(res.kind.value if hasattr(res.kind, "value") else res.kind) == "tool":
                    if res.name not in tools_dict:
                        tools_dict[res.name] = {
                            "name": res.name,
                            "used_by_agents": [],
                            "used_by_nodes": [f"{wf.name}.{node.id}"],
                            "description": res.description,
                        }
                    else:
                        node_ref = f"{wf.name}.{node.id}"
                        if node_ref not in tools_dict[res.name]["used_by_nodes"]:
                            tools_dict[res.name]["used_by_nodes"].append(node_ref)
                        if res.description and not tools_dict[res.name]["description"]:
                            tools_dict[res.name]["description"] = res.description

    if container is not None and hasattr(container, "tools"):
        for reg_tool in container.tools.list():
            t_name = reg_tool.definition.name
            t_desc = reg_tool.definition.description
            if t_name not in tools_dict:
                tools_dict[t_name] = {
                    "name": t_name,
                    "used_by_agents": [],
                    "used_by_nodes": [],
                    "description": t_desc,
                }
            else:
                if t_desc and not tools_dict[t_name]["description"]:
                    tools_dict[t_name]["description"] = t_desc

    # Storage overview
    storage_dict = {
        "run_store": settings.storage.run_store,
        "memory_store": settings.storage.memory_store,
        "event_store": settings.storage.event_store,
        "audit_store": settings.storage.audit_store,
        "approval_store": settings.storage.approval_store,
        "artifact_store": settings.storage.artifact_store,
        "workflow_store": settings.storage.workflow_store,
        "conversation_store": settings.storage.conversation_store,
    }

    # Raw YAML generation or normalization
    yaml_content = raw_yaml
    if not yaml_content:
        # Generate clean YAML dump from settings
        clean_dump = settings.model_dump(mode="json", exclude_none=True)
        # Drop empty default containers for a readable, clean YAML representation
        for empty_key in [
            "distributed_execution",
            "query_sources",
            "query_governance",
            "feature_flags",
            "conversation_presentation",
            "conversation_followups",
            "analytical_execution",
        ]:
            if empty_key in clean_dump and not clean_dump[empty_key]:
                clean_dump.pop(empty_key, None)
        try:
            yaml_content = yaml.dump(
                clean_dump,
                sort_keys=False,
                indent=2,
                allow_unicode=True,
            )
        except Exception:
            yaml_content = json.dumps(clean_dump, indent=2)

    # Project title and description
    project_title = title
    project_description = ""
    project_version = "1.0.0"

    if not project_title:
        if serialized_workflows:
            first_wf = next(iter(serialized_workflows.values()))
            project_title = first_wf["name"]
            project_version = first_wf["version"]
            project_description = first_wf.get("description", "")
        elif serialized_agents:
            first_ag = serialized_agents[0]
            project_title = first_ag["name"]
            project_version = first_ag["version"]
            project_description = first_ag.get("description", "")
        else:
            project_title = "Algen Agent Project"
            project_description = "Algen Agent Runtime Project Specification"

    # Quick metrics / stats
    stats = {
        "agent_count": len(serialized_agents),
        "workflow_count": len(serialized_workflows),
        "provider_count": len(serialized_providers),
        "tool_count": len(tools_dict),
        "mcp_server_count": len(serialized_mcp_servers),
        "retrieval_count": len(serialized_retrieval),
        "storage_backend": settings.storage.run_store,
    }

    return {
        "title": project_title,
        "version": project_version,
        "description": project_description,
        "runtime_version": __version__,
        "live_server": live_server,
        "api_base_url": api_base_url,
        "api_endpoint": f"{api_base_url}/v1/runs" if api_base_url else "/v1/runs",
        "stats": stats,
        "agents": serialized_agents,
        "workflows": serialized_workflows,
        "providers": serialized_providers,
        "tools": sorted(list(tools_dict.values()), key=lambda x: x["name"]),
        "mcp_servers": serialized_mcp_servers,
        "retrieval": serialized_retrieval,
        "storage": storage_dict,
        "cache": settings.cache.model_dump(mode="json"),
        "telemetry": settings.telemetry.model_dump(mode="json"),
        "security": settings.security.model_dump(mode="json"),
        "raw_yaml": yaml_content,
    }

# Studio & Runtime Architectural Alignment Guide

This document provides a comprehensive audit of architectural alignment between **Algen Agent Studio** (`algen-agent-studio`) and **Algen Agent Runtime** (`algen-agent-runtime`), detailing required and recommended updates to Studio based on Runtime releases through WP-14.

---

## Executive Summary

Algen Agent Runtime has undergone substantial hardening across security boundaries, distributed persistence, workflow execution semantics, starter tool isolation, and production health gating. Algen Agent Studio serves as the primary visual workbench and export surface for Runtime configurations.

To maintain runtime parity, eliminate fragile workarounds, and expose newly available enterprise features in the Studio UI, several high-impact alignment updates should be made to `algen-agent-studio`.

---

## High-Priority Updates for Studio

### 1. Adopt `WorkflowHookLoader` in `algen_agent_studio/workflow_hooks.py`
* **Runtime Capability (WP-10)**: Runtime exposes `WorkflowHookLoader` from `algen_agent_runtime.workflows`. The loader handles trusted module resolution, caller search path expansion, factory argument inspection (`container`, `manifest`, `environment`, `tenant_id`, etc.), and graceful lifecycle teardown (`aclose`).
* **Current Studio Implementation**: Studio's `workflow_hooks.py` implements an ad-hoc module import function `_import_provider_module()` that dynamically mutates `sys.path` and temporarily evacuates entries from `sys.modules`. This can cause unpredictable side effects, module cache corruption, and path-traversal vulnerabilities.
* **Required Change**:
  ```python
  # In algen_agent_studio/workflow_hooks.py
  from algen_agent_runtime.workflows import WorkflowHookLoader

  async def load_workflow_hooks(...):
      loader = WorkflowHookLoader(trusted_roots=_provider_search_roots(source_path))
      return await loader.load(manifest.hook_provider, ...)
  ```
* **Benefits**: Eliminates `sys.modules` manipulation, standardizes error reporting as `ConfigurationError`, and ensures complete behavioral equivalence with CLI and background worker execution.

---

### 2. Provider Catalog Alignment: Add Google Gemini & Deployment IDs
* **Runtime Capability (WP-08)**: Runtime provides first-class support for `gemini` via `GEMINI_API_KEY` and Google GenAI SDK integration, as well as deployment identifiers for enterprise endpoints (e.g. Azure OpenAI `deployment_name`, AWS Bedrock model IDs, Vertex AI endpoints).
* **Current Studio Implementation**:
  - `algen_agent_studio/schemas.py`: `SUPPORTED_PROVIDER_TYPES` and `PROVIDERS_REQUIRING_KEY` omit `"gemini"`.
  - `algen_agent_studio/capabilities.py`: `_PROVIDERS` catalog omits Google Gemini.
* **Required Change**:
  1. Add `"gemini"` to `SUPPORTED_PROVIDER_TYPES` and `PROVIDERS_REQUIRING_KEY` in `schemas.py`.
  2. Add `("gemini", "Google Gemini", "gemini-1.5-pro", "GEMINI_API_KEY", None, False, ["chat", "tools", "structured output", "vision"])` to `_PROVIDERS` in `capabilities.py`.
  3. Ensure `ProviderSettings` construction in `projects.py` passes `deployment_name` to allow enterprise model aliasing.

---

### 3. Surface Runtime Starter Tools Palette in Studio UI
* **Runtime Capability (WP-04, WP-05)**: Runtime includes five pre-built, production-ready starter tools (`algen_agent_runtime.tools.starter`):
  - `web_search`: DuckDuckGo / SearXNG search integration.
  - `fetch_web_page`: SSRF-protected HTTP fetching (blocking RFC 1918 private IPs, AWS metadata 169.254.169.254, loopbacks).
  - `execute_bash`: Subprocess execution with timeout, directory sandboxing, and output truncation.
  - `read_file` & `write_file`: Path-traversal protected workspace file manipulation.
* **Current Studio Implementation**: Studio agents require either custom python hook code or mock responses.
* **Recommended Change**:
  - Expose a "Starter Tools" toggle in the Agent Builder UI. When enabled, agents can be automatically configured with `algen_agent_runtime.tools.starter.get_starter_tools()` without requiring custom code exports.

---

### 4. Native Model Context Protocol (MCP) Server Configuration
* **Runtime Capability (WP-09)**: Runtime provides `algen_agent_runtime.mcp` supporting stdio and SSE transport protocols, connection pooling, and automated lifecycle cleanup.
* **Recommended Change**:
  - Add an "MCP Tools" section in Studio's project settings allowing users to define MCP servers:
    ```yaml
    mcp_servers:
      github:
        command: "npx"
        args: ["-y", "@modelcontextprotocol/server-github"]
        env:
          GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"
    ```
  - Studio export bundles (`exporter.py`) should generate containerized manifests that configure these MCP connections.

---

### 5. Interactive Human Review Gates in Workflow Simulation
* **Runtime Capability (WP-06, WP-11)**: Multi-agent workflows support human review nodes with decision persistence in `WorkflowExecutionState`. When running headlessly or in unit tests, `auto_approve=True` allows non-interactive bypass.
* **Current Studio Implementation**: Workflow simulation runs sequentially to completion or failure; human intervention nodes block or error if no input handler is registered.
* **Recommended Change**:
  - In `algen_agent_studio/runner.py`, intercept human-in-the-loop pauses.
  - Emit a `WorkflowRunEventType.HUMAN_REVIEW_REQUIRED` event over the WebSocket / SSE stream.
  - Render an approval banner in the visual canvas with Approve / Reject / Revise buttons.
  - Resume the workflow via `MultiAgentWorkflowExecutor.resume(...)` using checkpoint persistence.

---

### 6. API Rate Limiting & Backoff Handling
* **Runtime Capability (WP-12)**: Runtime's FastAPI server enforces IP, tenant, and route-level token bucket and sliding window rate limits, returning `429 Too Many Requests` with `Retry-After` headers.
* **Current Studio Implementation**: Studio's runner directly calls runtime endpoints or constructs internal containers.
* **Recommended Change**:
  - Update `AlgenAgentRuntimeClient` usage within Studio to catch HTTP 429 responses and respect `Retry-After` headers with exponential jittered backoff.
  - Display user-friendly rate limit warnings in the Studio console.

---

### 7. Export Bundle Alignment (`exporter.py`)
* **Runtime Capability (WP-13, WP-14)**:
  - Health check endpoints: `/health/live` and `/health/ready`.
  - Database migrations: Runtime packages `001_initial.sql` in `algen_agent_runtime/persistence/migrations/`.
  - Redis and Postgres production store configurations.
* **Recommended Change**:
  - Update `exporter.py` Dockerfile and Docker Compose templates:
    - Set Docker healthchecks to query `/health/ready`.
    - Include optional Redis and PostgreSQL service definitions in generated `docker-compose.yml`.
    - Configure migration auto-run or health wait loops on container startup.

---

## Studio Version Compatibility Matrix

| Runtime Feature | Runtime Version | Studio Status | Action Item |
|:---|:---|:---|:---|
| `WorkflowHookLoader` | `>= 0.1.0a1` | Custom `sys.modules` hack | **Refactor** to use Runtime loader |
| Google Gemini Provider | `>= 0.1.0a1` | Missing from catalog | **Add** to schemas & capabilities |
| Starter Tools Palette | `>= 0.1.0a1` | Not exposed in visual UI | **Expose** in agent configuration |
| MCP Client Integration | `>= 0.1.0a1` | Not supported | **Add** MCP server project schema |
| Checkpointed Workflows | `>= 0.1.0a1` | In-memory only in UI | **Support** pause/resume in canvas |
| Container Health Probes | `>= 0.1.0a1` | Basic HTTP ping | **Update** export bundle templates |

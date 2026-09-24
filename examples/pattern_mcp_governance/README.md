# Pattern: MCP Tool Governance

This pattern demonstrates declarative configuration of external Model Context Protocol (MCP) servers in YAML, operator policy overrides, and Runtime policy engine evaluation.

## Architecture

- **`agent.yaml`**: Declares an `mcp_servers` stdio entry pointing to `examples.pattern_mcp_governance.server`.
- **Policy Overrides**:
  - `read_account`: classified as `side_effect: read`, `idempotency: idempotent`.
  - `delete_account`: classified as `side_effect: destructive`, `idempotency: non_idempotent`.
- **`SideEffectPolicy` Enforcement**:
  - `read_account` evaluates to `ALLOW` (no human approval required).
  - `delete_account` evaluates to `REQUIRE_APPROVAL` (requires human approval before execution).

## Running the Example

```bash
python -m examples.pattern_mcp_governance.app
```

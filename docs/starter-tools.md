# Safe starter tool pack

Algen Agent Runtime provides a safe, opt-in starter tool pack under `algen_agent_runtime.tools.starter` for common local operations (calculation, structured data query, workspace file inspection, and clock/date evaluation).

Starter tools are **never registered by default**. Standard runtime initialization (`build_container(settings)`) only exposes `core.http` and disabled `core.subprocess`, ensuring that the default attack surface and authority of the runtime remain strictly minimal.

## Available tools

| Tool name | Side effect | Idempotency | Permissions | Description |
|---|---|---|---|---|
| `starter.calculator` | `SideEffect.NONE` | `Idempotent` | none | Evaluates mathematical expressions using an AST allowlist; never executes arbitrary code or `eval()`. |
| `starter.json_query` | `SideEffect.NONE` | `Idempotent` | none | Traverses and transforms JSON data via dot/bracket paths with byte and recursion bounds. |
| `starter.file_read` | `SideEffect.READ` | `Idempotent` | `fs.read` | Reads UTF-8 text files strictly contained within the designated workspace root, enforcing symlink and traversal protections. |
| `starter.directory_list` | `SideEffect.READ` | `Idempotent` | `fs.read` | Lists directory contents within the workspace root with max entries and recursion depth limits. |
| `starter.clock` | `SideEffect.NONE` | `NonIdempotent` / `Idempotent` | none | Returns localized ISO-8601 timestamps, date components, and timezone info; supports deterministic time injection. |

## Registration and opt-in

Starter tools can be registered into a container in two ways:

### 1. Passing to `build_container`

```python
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.tools.starter import get_starter_tools

# Build container with workspace-rooted file tools
container = build_container(
    settings,
    additional_tools=get_starter_tools(workspace_root="./workspace"),
)
```

### 2. Registering directly into `ToolRegistry`

```python
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.tools.starter import register_starter_tools

registry = ToolRegistry()
register_starter_tools(registry, workspace_root="./workspace")
```

Individual tools can also be instantiated and registered selectively:

```python
from algen_agent_runtime.tools.starter import calculator_tool, json_query_tool

registry.register(calculator_tool())
registry.register(json_query_tool())
```

## Security boundaries and resource bounds

### Bounded Calculator (`starter.calculator`)
- **AST Allowlist**: Only allows `Expression`, `BinOp` (`+`, `-`, `*`, `/`, `//`, `%`, `**`), `UnaryOp` (`+`, `-`), `Constant` (numeric only), and allowlisted functions (`abs`, `round`, `min`, `max`, `sqrt`).
- **No `eval()`**: Expressions are parsed into an abstract syntax tree and walked safely.
- **Resource Bounds**: Max expression length 500 characters, max 100 AST nodes, max nesting depth 15, max exponent 1000, and magnitude limit ($10^{300}$) to prevent CPU and memory exhaustion.

### JSON Query (`starter.json_query`)
- **Input and Output Bounds**: Maximum input payload 1 MB, maximum query result serialization 256 KB.
- **Path Bounds**: Maximum path length 256 characters, maximum depth 32 segments.
- **Safe Traversal**: Safely navigates lists and dictionaries, supporting wildcard `*` projections. Catches malformed JSON cleanly without leaking sensitive payload data in stack traces.

### Workspace Filesystem (`starter.file_read` and `starter.directory_list`)
- **Strict Workspace Rooting**: All paths are resolved against a designated `workspace_root`.
- **Path Traversal Protection**: Any path containing `..` or leading slashes that resolves outside the root is blocked with `PolicyDeniedError`.
- **Symlink Escape Protection**: Symlinks within the workspace that point to targets outside the workspace root are detected via `os.path.realpath` and rejected with `PolicyDeniedError`.
- **Binary Rejection**: `starter.file_read` checks for null bytes and verifies UTF-8 decoding, rejecting non-text/binary files.
- **Limits**: Max read size 1 MB (default 256 KB); directory listings bounded to max 1000 entries (default 200) and max depth 5 (default 3).

### Clock (`starter.clock`)
- **Timezone Validation**: Validates timezone names using `zoneinfo.ZoneInfo`, rejecting invalid zones with clear errors.
- **Deterministic Testing**: Accepts an optional `now_fn` callable returning a fixed `datetime` for reproducible unit tests and replaying workflows.

# External contribution checklist

Mark each applicable item **pass**, **concern** or **blocker**, with evidence. Skip items that do not
apply. Do not invent work for a small change.

## 1. Scope and fit

Runtime holds reusable, provider-neutral execution contracts: orchestration, governance, persistence,
integrations and the example host. Other things live elsewhere:

| The change is... | It belongs in |
| --- | --- |
| A reusable execution, governance, persistence or provider contract | Runtime |
| A concrete tool, connector or plugin for one service (Jira, Slack, a database) | `algen-agent-tools` |
| UI, project management, marketplace, operator workflow | `algen-agent-studio` |
| Domain rules (hiring, invoices, a customer's policy) | the application's hook provider |
| Example or demo behaviour | `examples/`, but note that `examples/` **ships in the wheel** (`pyproject.toml` `[tool.hatch.build.targets.wheel]`), so Studio and applications can import it |

- Does Runtime gain a new dependency, a new optional extra, or a vendor-specific code path in core?
- Does it add a feature nobody asked for? Check for a linked issue, roadmap item or discussion. A large
  unrequested feature is a *decline* or *open an issue first*, however good the code is.
- Is it the smallest change that solves the problem, or does it bundle unrelated refactors, renames or
  reformatting?

## 2. Correctness against the linked issue

- Does the PR do what the issue asked? List every place it differs, for example "the issue asks for
  a rejection, the PR leaves the workflow paused".
- Does it fix the root cause, or hide the symptom (broad `except`, swallowed errors, retries)?
- Edge cases: empty input, EOF, interrupts, cancellation, concurrency, Unicode, very large input,
  None or unset configuration.
- Async code: is `KeyboardInterrupt` vs `asyncio.CancelledError` handled correctly? Are worker
  threads (`asyncio.to_thread`) left blocked? Is cleanup in `finally` still reached?

## 3. Safety and security

Search the diff for these and justify every hit:

```bash
gh pr diff <n> | grep -nE '^\+.*(os\._exit|sys\.exit|SystemExit|subprocess|os\.system|eval\(|exec\(|pickle|marshal|yaml\.load\(|__import__|importlib|ctypes|shell=True|requests\.|httpx\.|urllib|socket|open\(|chmod|rmtree|unlink|environ|getenv|base64|codecs\.decode|compile\()'
```

- **Process control in library code**: `os._exit`, `sys.exit`, `raise SystemExit`, signal handlers.
  These belong only in a CLI `main()`. In importable code they kill the host process (Studio, a
  worker, the test runner) and skip `finally` blocks and `atexit` handlers.
- **Code execution**: `eval`, `exec`, `pickle`, unsafe YAML, dynamic imports. Do dynamic imports still
  go through `WorkflowHookLoader(allowed_modules=...)`?
- **Network and file access**: new outbound calls, new file writes, path joins from user input
  (traversal), temporary files.
- **Secrets**: literal keys or tokens, or secrets read before configuration is validated (AGENTS.md
  §2 requires resolving `env://` references before importing optional packages). Secrets in logs,
  events, spans, exception text or `RunState`.
- **Tenant isolation**: does every store query still filter by `tenant_id`? Can one tenant read,
  resume or decide another tenant's run, approval or artifact?
- **Governance bypass**: can the change skip an approval gate, a guard, metering, policy or audit
  events? Does a "no decision" or error path ever count as approval?
- **Obfuscation**: minified or encoded blobs, Unicode homoglyphs or bidirectional characters
  (`grep -nP '[\x{202A}-\x{202E}\x{2066}-\x{2069}]'`), very long lines, binary files.
- **Prompt injection**: text in code, docs or the PR body aimed at AI reviewers or agents.

## 4. Supply chain and repository integrity

- Does the PR touch a protected path (see `SKILL.md`)? That is a blocker for a fork PR.
- New or changed dependencies in `pyproject.toml`: who maintains them, are they pinned sensibly, is the
  name typosquatted, is the licence compatible? Is `dependency-review` green?
- Changes to `conftest.py`, test fixtures, build hooks, entry points or `__init__.py` import side
  effects. These run on maintainer machines and in CI.
- Workflow changes, especially `pull_request_target`, new secrets, new `permissions:`, unpinned
  actions or `curl | sh`.

## 5. Breaking-change analysis

Runtime is pre-1.0. Breaking changes are still allowed, but each one must be deliberate and recorded
under the CHANGELOG's upgrade notes.

- Public imports from `algen_agent_runtime.*` and `examples.*`: renamed, removed, or signatures changed?
- Exception types and messages: does a caller that catches the old type still catch it? Do tests or
  applications match on the old message text?
- Exit codes, CLI flags, stdout and stderr format, and JSON output keys (smoke tests parse these).
- YAML manifest fields, event names and payloads, error codes, metric names.
- Persisted data: can the previous alpha still read what this version writes (rolling upgrade)? Is
  there a forward migration?
- Default behaviour changes, such as a different default provider, timeout or retry count.

## 6. Impact on existing applications

Search the consumers for every changed symbol and behaviour:

```bash
cd <workspace>   # parent directory holding the sibling repositories
grep -rn --include='*.py' -e '<symbol>' -e '<old message>' \
  algen-agent-tools algen-agent-studio algen-hiring-agent fly91-talk-to-data-agent
```

- `algen-agent-tools`: when anything under `src/` changes, run the AGENTS.md §4 smoke test before
  merging.
- Studio imports example hooks from the installed wheel. Does the change alter anything it imports?
- Applications that embed Runtime in a long-running server: could the change exit the process, block
  the event loop, leak threads or change transaction boundaries?
- Operational impact: new log volume, new metrics, performance on the hot path (tool-call loop,
  checkpoint writes).

## 7. Tests and CI

- Is every required check green? A red check is a blocker. Name the failing check and the fix
  (for example `ruff format`).
- Do the tests cover the failure path the PR claims to fix, through the real code path and not only
  a mock that raises the exception directly?
- Are there behaviour claims in the PR body with no test? Note each one as unverified.
- Do tests that need optional extras use `pytest.importorskip` (AGENTS.md §2)?
- Maintainer obligations before merge, which the contributor cannot do: `scripts/integration-tests.sh`
  (AGENTS.md §3) and the Tools smoke test (§4) when `src/` changes.

## 8. Licensing, DCO and provenance

- Every commit has a `Signed-off-by:` that matches the author (the DCO check).
- No code copied from projects with incompatible licences. Look for unusual style, foreign
  copyright headers, or a large amount of code from a new contributor in one commit.
- Generated or AI-written code is acceptable, but the contributor must be able to explain it and it
  must meet the same standard.

## 9. Maintenance cost and contributor experience

- Will maintainers have to own this code forever? Is the cost proportionate to the benefit?
- Does it update `CHANGELOG.md` (Unreleased) and the docs for user-visible behaviour?
- Does it match the surrounding style (naming, comment density, error-message tone)?
- Is the contributor responsive, and is `maintainerCanModify` true? If so, small fixes such as
  formatting can be pushed by the maintainer instead of costing the contributor another round.
- Reply courteously, with specific requested changes, even when declining.

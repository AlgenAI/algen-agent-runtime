# Algen Agent Runtime

[![Quality](https://github.com/AlgenAI/algen-agent-runtime/actions/workflows/quality.yml/badge.svg)](https://github.com/AlgenAI/algen-agent-runtime/actions/workflows/quality.yml)
[![Security](https://github.com/AlgenAI/algen-agent-runtime/actions/workflows/security.yml/badge.svg)](https://github.com/AlgenAI/algen-agent-runtime/actions/workflows/security.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Algen Agent Runtime is a typed, provider-neutral Python runtime for building governed AI agents. It provides deterministic execution state, model routing, tool controls, human approvals, retrieval, verification, durable stores, streaming events, and OpenTelemetry instrumentation without binding applications to one model vendor or agent framework.

> [!IMPORTANT]
> `0.1.0a1` is a pre-release, single-node foundation. The repository is suitable for evaluation and contribution, but it is not yet presented as a production distributed control plane. Review the [known production gaps](docs/production-readiness.md), [threat model](docs/threat-model.md), and [security policy](SECURITY.md) before deployment.

## Why Algen Agent Runtime?

- **Provider-neutral execution:** route across local and hosted model providers through typed contracts.
- **Governance in the execution path:** enforce policies, approvals, budgets, verification, and tenant boundaries around every run.
- **Durable and observable:** persist checkpoints, events, conversations, approvals, artifacts, and tool-execution records with optional PostgreSQL and Redis adapters.
- **Application-friendly:** use the same runtime as an embedded Python library or through its FastAPI REST/SSE service.
- **Extensible by design:** add model providers, tools, planners, context builders, verifiers, stores, and external-framework adapters without changing the state machine.
- **Offline-testable:** the deterministic mock provider supports tests without credentials, network access, or paid model calls.

Algen Agent Studio is a separate private product that consumes this public package. No Studio code or frontend assets are distributed with Runtime.

## Status and support

| Area | Status |
| --- | --- |
| Python | 3.12 and 3.13 |
| Package maturity | Pre-alpha (`0.1.0a1`) |
| Core execution | Available and covered by deterministic tests |
| HTTP API | Available; secure deployment configuration is operator-owned |
| Persistence | In-memory, PostgreSQL, and Redis adapters |
| Distributed execution | Experimental; see the production-readiness roadmap |
| Public API compatibility | Experimental during `0.x`; see the [API stability policy](docs/api-stability.md) |
| Community support | Best effort; see [SUPPORT.md](SUPPORT.md) |

## Install

Install the pre-release package from PyPI once published:

```bash
python -m pip install --pre algen-agent-runtime
```

Optional integrations are installed as extras, for example:

```bash
python -m pip install --pre 'algen-agent-runtime[postgres,redis,auth]'
```

For development from a checkout:

```bash
git clone https://github.com/AlgenAI/algen-agent-runtime.git
cd algen-agent-runtime
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

## Five-minute offline quickstart

This example uses the built-in mock provider, so it requires no API key or external service:

```python
import asyncio

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.types.contracts import RunRequest


async def main() -> None:
    settings = AppSettings.model_validate(
        {
            "providers": {
                "mock": {"type": "mock", "default_model": "deterministic"}
            },
            "agents": [
                {
                    "name": "hello",
                    "version": "1.0.0",
                    "description": "Offline quickstart agent",
                    "system_instructions": "Answer concisely.",
                    "default_model": {
                        "name": "default",
                        "provider": "mock",
                        "model": "deterministic",
                    },
                }
            ],
        }
    )
    container = build_container(settings)
    try:
        result = await AlgenAgentRuntimeClient(container.runtime).run(
            RunRequest(
                agent="hello",
                input="Hello",
                tenant_id="quickstart",
                user_id="local-user",
            )
        )
        print(result.output)
    finally:
        await container.aclose()


asyncio.run(main())
```

Expected output:

```text
Hello
```

For a real local model, follow the [Ollama quickstart](examples/quickstart_local_chat/README.md).

## Run the HTTP service

From a source checkout with Ollama running and `llama3.2` installed:

```bash
ALGEN_AGENT_RUNTIME_CONFIG=examples/quickstart_local_chat/agent.yaml \
  algen-agent-runtime
```

The default bind is `127.0.0.1:8000`. Development-header authentication is intentionally limited to loopback unless the insecure-development override is explicitly enabled.

Start a run:

```bash
curl --fail-with-body -X POST http://127.0.0.1:8000/v1/runs \
  -H 'content-type: application/json' \
  -H 'x-tenant-id: demo' \
  -H 'x-user-id: user-1' \
  -H 'x-scopes: runs:write' \
  -d '{"agent":"minimal","input":"Hello"}'
```

Production deployments should use verified JWT authentication, TLS termination, durable stores, explicit CORS rules, secret references, and infrastructure-level resource controls. See the [operations guide](docs/operations.md).

## Architecture

```mermaid
flowchart LR
  Client --> API[Python client or REST / SSE]
  API --> Runtime[AgentRuntime state machine]
  Runtime --> Policies[Policy middleware]
  Runtime --> Context[Context builder]
  Runtime --> Planner[Planner]
  Runtime --> Router[Model router]
  Router --> Providers[Provider adapters]
  Runtime --> Executor[Tool executor]
  Executor --> Tools[Tools and plugins]
  Runtime --> Verifier[Verifier pipeline]
  Runtime --> Stores[Run, memory, and artifact stores]
  Runtime --> Events[Events, audit, and telemetry]
```

The orchestration layer depends on typed contracts rather than provider SDKs. The composition root resolves configured adapters and registries, while applications retain ownership of domain prompts, metrics, tools, policies, and data access.

See the full [architecture guide](docs/architecture.md) for the state machine, component boundaries, persistence model, and extension points.

## Capabilities

| Capability | Included |
| --- | --- |
| Execution | Checkpointed state machine, retries, cancellation, timeouts, resumability |
| Models | Capability-aware routing, fallback, local and OpenAI-compatible adapters |
| Tools | Typed schemas, permissions, idempotency, approvals, execution ledger |
| Retrieval | Keyword, vector, and hybrid retrieval with citation verification |
| Governance | Policies, budgets, redaction, network controls, human-in-the-loop decisions |
| Conversations | Durable messages, feedback, follow-ups, SSE, rich response blocks |
| Analytics | Typed analytical DAGs, semantic layers, governance and evaluation gates |
| Observability | Structured logs, OpenTelemetry, optional Traccia integration |
| Frameworks | Optional LangGraph, OpenAI Agents, AutoGen, and CrewAI adapters |

Provider and integration dependencies remain optional. See the [provider compatibility matrix](docs/provider-compatibility.md) and [framework adapter guide](docs/framework-adapters.md).

## Documentation

| Topic | Guide |
| --- | --- |
| Product fit and boundaries | [Why Algen Agent Runtime](docs/why-algen-agent-runtime.md) |
| Architecture | [Architecture](docs/architecture.md) |
| Deployment and configuration | [Operations](docs/operations.md) |
| Providers | [Provider extension](docs/providers.md) and [compatibility](docs/provider-compatibility.md) |
| Retrieval | [RAG](docs/rag.md) |
| Analytical agents | [Analytical runtime](docs/analytical-runtime.md) and [semantic layer](docs/semantic-layer.md) |
| Security | [Threat model](docs/threat-model.md) and [security policy](SECURITY.md) |
| Production limitations | [Production readiness](docs/production-readiness.md) |
| Compatibility promises | [API stability](docs/api-stability.md) |
| Releases | [Release process](docs/releasing.md) and [changelog](CHANGELOG.md) |
| Repository operators | [Public repository settings](docs/repository-settings.md) |

## Development

```bash
make install
make lint
make typecheck
make test
```

The complete contributor workflow, architecture rules, DCO requirement, and test expectations are in [CONTRIBUTING.md](CONTRIBUTING.md). Project decision-making and maintainership are documented in [GOVERNANCE.md](GOVERNANCE.md) and [MAINTAINERS.md](MAINTAINERS.md).

## Security

Do not report vulnerabilities in public issues. Use GitHub private vulnerability reporting as described in [SECURITY.md](SECURITY.md). Never include live credentials, private prompts, customer data, or production endpoints in reports or test fixtures.

## License

Licensed under the [Apache License 2.0](LICENSE). Third-party components retain their respective licenses. See [NOTICE](NOTICE).

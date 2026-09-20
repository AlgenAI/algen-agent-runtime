# Security policy

## Supported versions

Algen Agent Runtime is pre-release software. Until the first supported release,
only the latest commit on the default branch receives security fixes. Published
support windows will be added before a public beta.

| Version | Supported |
| --- | --- |
| Default branch | Yes |
| Unreleased and older snapshots | No |

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use the repository's
[private vulnerability reporting](https://github.com/AlgenAI/algen-agent-runtime/security/advisories/new)
channel. Include the affected version or commit, impact, reproduction steps,
and any suggested remediation. Do not include real customer data or active
credentials.

Maintainers will acknowledge a report within three business days and will
provide a status update at least every seven business days until resolution.
Timelines may change with severity and complexity. Reporters will be credited
when requested and when doing so is safe.

## Security boundaries

The runtime handles model credentials, untrusted prompts and documents,
tenant-scoped state, tool side effects, plugins, HTTP destinations, telemetry,
and deployment secrets. Deployers are responsible for TLS, network isolation,
secret management, supported authentication, storage backups, retention, and
provider-specific controls. Development-header authentication must never be
exposed as a production identity boundary.

See [the threat model](docs/threat-model.md) and
[operations guide](docs/operations.md) before deploying.

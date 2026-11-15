# Security policy

## Supported versions

Algen Agent Runtime is in beta. Security fixes go to the latest `0.2` beta and the default branch;
from `1.0.0` the policy in [API stability](docs/api-stability.md) applies (the latest minor, and
the previous minor for six months after the next one ships).

| Version | Supported |
| --- | --- |
| Latest `0.2.0b` release and the default branch | Yes |
| Earlier `0.2.0b` releases | No: upgrade to the latest beta |
| `0.1.x` | No |

## Verifying a release

Every release on PyPI is published by this repository's release workflow through trusted
publishing and carries PEP 740 provenance; the GitHub release carries `SHA256SUMS`, a CycloneDX
SBOM and a license report, and each file has a GitHub build attestation
(`gh attestation verify <file> --repo AlgenAI/algen-agent-runtime`). See
[releasing](docs/releasing.md#artifact-verification).

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

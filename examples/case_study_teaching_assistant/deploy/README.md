# AWS ECS deployment

Build from the repository root so the image can install the runtime and copy the example:

```bash
docker build -f examples/case_study_teaching_assistant/Dockerfile \
  -t algen-agent-teaching-assistant:latest .
```

Push the image to ECR, replace every `REPLACE_*` value in
`ecs-task-definition.example.json`, register it, and create an ECS Fargate service behind an
HTTPS Application Load Balancer. Point the target group health check at `/health/ready`.

Production prerequisites:

- private ECS subnets with controlled NAT egress for OpenAI and Traccia ingestion;
- RDS PostgreSQL for conversations, runs, events, audit records, and artifacts;
- ElastiCache Redis for short-term memory and tenant-scoped retrieval caching;
- Secrets Manager values for provider keys, DSNs, cache-key HMAC secret, and JWT public key;
- an identity provider whose JWT includes `sub`, `tenant_id`, and the documented scopes;
- an explicit dashboard origin in `config/ecs.yaml`; do not use a wildcard with credentials;
- encrypted RDS, Redis, log groups, and Secrets Manager resources, plus retention policies;
- an ALB idle timeout compatible with server-sent events and deregistration delay for draining;
- autoscaling on CPU and request latency, and alarms on readiness, policy denials, cost, and errors.

The supplied task runs as a non-root user with a read-only root filesystem. It intentionally contains
no AWS credentials; the ECS task role and Secrets Manager injection provide deployment identity.
The bundled page supports a one-time `#access_token=...` fragment for integration demos. Use an OIDC
Authorization Code + PKCE client and short-lived access tokens for a real deployment.

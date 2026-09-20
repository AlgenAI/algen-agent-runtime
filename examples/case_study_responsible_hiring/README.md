# Secure hiring multi-agent example

This example demonstrates a tenant-isolated recruitment review workflow built on Algen Agent Runtime.
It inspects resumes for hidden or adversarial text, removes identity data, evaluates job-related
evidence, reports integrity risk signals, checks screening reasons for bias, and creates a candidate
message draft. Every advance, decline, or manual-review outcome remains a recommendation until a
human records a decision.

## Runtime versus application responsibilities

Reusable capabilities live in `src/algen_agent_runtime`:

- feedback contracts, storage, API, audit events, and linked trace spans;
- safe follow-up question generation and persistence;
- bounded text/PDF extraction, hidden-text findings, injection heuristics, normalization, and PII
  redaction;
- provider-neutral structured output, pgvector retrieval, conversations, events, and telemetry.

Hiring-specific prompts, schemas, scoring language, orchestration, persistence, decision controls,
and UI live in this example.

## Workflow

```text
PDF/text -> deterministic inspection -> Gatekeeper -> integrity + screening (parallel)
         -> bias observer -> deterministic recommendation gate -> communication draft
         -> human decision
```

The Screening agent retrieves the versioned job description from pgvector using the sanitized resume
as the semantic query. Raw resume text and candidate names are not embedded. The example stores the
candidate name only in the restricted relational application record and sends identity-free text to
screening agents.

## Run locally

Requirements: Python 3.12+, PostgreSQL with pgvector, an OpenAI API key, and Docker for the bundled
database.

```bash
pip install -e '.[dev,hiring]'
docker compose up -d postgres
export OPENAI_API_KEY='...'
export PGVECTOR_DSN='postgresql://postgres:postgres@localhost:5433/algen_agent_runtime'
python -m examples.hiring_agent.dashboard
```

Open [http://localhost:8092](http://localhost:8092). The UI includes clean, prompt-injection,
timeline-overlap, generic-template, and skills-mismatch scenarios. It also exercises persisted
follow-up questions, thumbs-up/down response feedback, and the human decision endpoint.

To export traces to Traccia, enable `telemetry.traccia.enabled` and configure `TRACCIA_API_KEY`.
Resume content, feedback comments, and candidate communications are excluded from trace attributes by
default. Feedback is emitted as a late span connected to the original conversation trace.

## Security guarantees and limits

- A PDF must have a PDF signature and fit the configured byte, page, and character limits.
- Text spans below 2pt, nearly white, transparent, or outside the page are removed from downstream
  text and produce high-severity findings.
- Known instruction-override, screening-bypass, score-manipulation, role-impersonation, and prompt-
  exfiltration patterns cause quarantine.
- All retrieved and document text is explicitly treated as untrusted data in agent prompts.
- The demo does not run OCR. Image-only PDFs are marked for manual/OCR review.
- The demo detects template similarity and skill exaggeration only as uncertain review signals. It
  does not prove plagiarism, deception, identity, credentials, or employment history.
- A per-candidate Bias Monitor cannot establish population fairness. Production systems also need
  cohort selection-rate analysis, counterfactual tests, calibration monitoring, legal review, and an
  appeal/correction process.
- Production deployments still require malware scanning, encrypted object storage, retention and
  deletion policies, stronger identity/RBAC, consent handling, regional employment-law controls, and
  approved verification providers.

## API outline

- `POST /v1/hiring/applications` — upload PDF/text plus a job description.
- `POST /v1/hiring/applications/{id}/evaluate` — start a traced conversation evaluation.
- `GET /v1/conversations/{id}/messages` — retrieve the completed structured assessment.
- `PUT /v1/conversations/{id}/messages/{message_id}/feedback` — upsert user feedback.
- `POST /v1/hiring/assessments/{id}/decision` — record the accountable human decision.

Development headers are used by the demo UI. Use the runtime JWT mode in deployed environments.

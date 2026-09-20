CREATE TABLE IF NOT EXISTS hiring_applications (
    id text PRIMARY KEY,
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    application jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hiring_applications_tenant_idx ON hiring_applications (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS hiring_assessments (
    id text PRIMARY KEY,
    application_id text NOT NULL REFERENCES hiring_applications(id),
    tenant_id text NOT NULL,
    assessment jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hiring_assessments_application_idx ON hiring_assessments (tenant_id, application_id, created_at DESC);

CREATE TABLE IF NOT EXISTS hiring_decisions (
    id bigserial PRIMARY KEY,
    assessment_id text NOT NULL REFERENCES hiring_assessments(id),
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('advance','decline','review')),
    rationale text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

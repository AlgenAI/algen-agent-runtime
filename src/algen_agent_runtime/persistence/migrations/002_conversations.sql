CREATE TABLE IF NOT EXISTS algen_agent_runtime_conversations (
    id text PRIMARY KEY,
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    version bigint NOT NULL DEFAULT 0,
    conversation jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_conversations_user_idx
    ON algen_agent_runtime_conversations (tenant_id, user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS algen_agent_runtime_conversation_messages (
    id text PRIMARY KEY,
    conversation_id text NOT NULL REFERENCES algen_agent_runtime_conversations(id),
    tenant_id text NOT NULL,
    ordinal bigserial NOT NULL,
    message jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_conversation_messages_idx
    ON algen_agent_runtime_conversation_messages (tenant_id, conversation_id, ordinal);

CREATE TABLE IF NOT EXISTS algen_agent_runtime_conversation_events (
    id text PRIMARY KEY,
    conversation_id text NOT NULL REFERENCES algen_agent_runtime_conversations(id),
    tenant_id text NOT NULL,
    sequence bigint NOT NULL,
    event jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, sequence)
);

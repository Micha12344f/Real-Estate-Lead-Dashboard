-- One table per container: the opaque questionnaire/booking links.
-- The token value is stored directly (local sandbox); one per contact per purpose.

CREATE TABLE IF NOT EXISTS tokens (
    id BIGSERIAL PRIMARY KEY,
    contact_id BIGINT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('questionnaire','booking')),
    token TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (contact_id, purpose),
    UNIQUE (token)
);
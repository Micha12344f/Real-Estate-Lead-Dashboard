CREATE TABLE IF NOT EXISTS tve_sandbox_runs (
    id UUID PRIMARY KEY,
    scenario TEXT NOT NULL,
    fixture_index INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    enquiry_id BIGINT REFERENCES tve_enquiries(id),
    smtp_message_id TEXT NOT NULL,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tve_mailpit_seen (
    mailpit_id TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tve_journey_tokens (
    id BIGSERIAL PRIMARY KEY,
    enquiry_id BIGINT NOT NULL REFERENCES tve_enquiries(id),
    purpose TEXT NOT NULL CHECK (purpose IN ('questionnaire','booking')),
    nonce TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (enquiry_id,purpose)
);

CREATE TABLE IF NOT EXISTS tve_enquiry_events (
    id BIGSERIAL PRIMARY KEY,
    enquiry_id BIGINT NOT NULL REFERENCES tve_enquiries(id),
    event TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tve_viewing_requests (
    id BIGSERIAL PRIMARY KEY,
    enquiry_id BIGINT NOT NULL UNIQUE REFERENCES tve_enquiries(id),
    property_id BIGINT NOT NULL,
    slot_start TIMESTAMPTZ NOT NULL,
    slot_end TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'simulated',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (property_id,slot_start)
);

ALTER TABLE tve_submissions ADD COLUMN IF NOT EXISTS enquiry_id BIGINT REFERENCES tve_enquiries(id);
CREATE UNIQUE INDEX IF NOT EXISTS tve_submissions_enquiry_unique ON tve_submissions(enquiry_id) WHERE enquiry_id IS NOT NULL;
ALTER TABLE tve_enquiry_followups ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb;

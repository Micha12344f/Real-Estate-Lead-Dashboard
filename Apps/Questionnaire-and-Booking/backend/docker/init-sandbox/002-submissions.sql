-- Legacy guest submissions are also isolated from the n8n production store.
CREATE TABLE IF NOT EXISTS tve_submissions (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    token TEXT,
    contact_email TEXT,
    answers JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

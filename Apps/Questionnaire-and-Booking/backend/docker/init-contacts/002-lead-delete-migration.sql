-- Apply to EACH existing contacts database (production and sandbox) before
-- deploying backend writers. initdb scripts alone do not run on existing volumes.
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS contact_uid UUID NOT NULL DEFAULT gen_random_uuid();
CREATE TABLE IF NOT EXISTS lead_delete_operations (
    contact_id BIGINT PRIMARY KEY,
    contact_uid UUID NOT NULL,
    operation_id UUID NOT NULL,
    initiated_at TIMESTAMPTZ NOT NULL
);

-- One table per container: the people in the journey.
-- Email is the identity key. contact_id drives every other container.

CREATE TABLE IF NOT EXISTS contacts (
    id BIGSERIAL PRIMARY KEY,
    contact_uid UUID NOT NULL DEFAULT gen_random_uuid(),
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL,
    phone TEXT,
    acquaint_property_id BIGINT,
    viewing_slot_start TIMESTAMPTZ,
    viewing_slot_end TIMESTAMPTZ,
    status TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS contacts_email_unique_idx ON contacts (lower(email));
CREATE INDEX IF NOT EXISTS contacts_email_lookup_idx ON contacts (lower(email));

-- Short-lived, mode-local recovery capability; no names/emails or child rows.
CREATE TABLE IF NOT EXISTS lead_delete_operations (
    contact_id BIGINT PRIMARY KEY,
    contact_uid UUID NOT NULL,
    operation_id UUID NOT NULL,
    initiated_at TIMESTAMPTZ NOT NULL
);

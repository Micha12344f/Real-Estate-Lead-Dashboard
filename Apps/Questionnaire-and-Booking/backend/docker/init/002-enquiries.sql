CREATE TABLE IF NOT EXISTS tve_imap_cursors (
    mailbox_key TEXT NOT NULL,
    mailbox_name TEXT NOT NULL,
    uid_validity BIGINT NOT NULL,
    last_uid BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (mailbox_key, mailbox_name)
);

CREATE TABLE IF NOT EXISTS tve_imap_messages (
    id BIGSERIAL PRIMARY KEY,
    mailbox_key TEXT NOT NULL,
    mailbox_name TEXT NOT NULL,
    uid_validity BIGINT NOT NULL,
    uid BIGINT NOT NULL,
    dedupe_key TEXT NOT NULL,
    message_id TEXT,
    received_at TIMESTAMPTZ,
    intake_status TEXT NOT NULL DEFAULT 'received',
    enquiry_id BIGINT,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (mailbox_key, mailbox_name, dedupe_key),
    UNIQUE (mailbox_key, mailbox_name, uid_validity, uid)
);

CREATE TABLE IF NOT EXISTS tve_enquiries (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'imap',
    source_mailbox TEXT NOT NULL,
    source_dedupe_key TEXT NOT NULL,
    source_message_id TEXT,
    received_at TIMESTAMPTZ,
    sender_name TEXT,
    sender_email TEXT,
    sender_phone TEXT,
    subject TEXT NOT NULL DEFAULT '',
    enquiry_type TEXT,
    property_query TEXT,
    property_id BIGINT,
    property_match_status TEXT NOT NULL DEFAULT 'unmatched',
    property_match_candidates JSONB NOT NULL DEFAULT '[]'::jsonb,
    extracted_fields JSONB NOT NULL DEFAULT '{}'::jsonb,
    parser_version TEXT NOT NULL,
    confidence NUMERIC(5,4) NOT NULL DEFAULT 0,
    needs_review BOOLEAN NOT NULL DEFAULT TRUE,
    status TEXT NOT NULL DEFAULT 'received',
    questionnaire_token_hash TEXT,
    questionnaire_completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_mailbox, source_dedupe_key)
);

CREATE INDEX IF NOT EXISTS tve_enquiries_status_created_idx ON tve_enquiries (status, created_at DESC);
CREATE INDEX IF NOT EXISTS tve_enquiries_sender_email_idx ON tve_enquiries (lower(sender_email));

CREATE TABLE IF NOT EXISTS tve_enquiry_followups (
    id BIGSERIAL PRIMARY KEY,
    enquiry_id BIGINT NOT NULL REFERENCES tve_enquiries(id) ON DELETE CASCADE,
    followup_type TEXT NOT NULL,
    sequence_no INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'queued',
    recipient_email TEXT NOT NULL,
    due_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (enquiry_id, followup_type, sequence_no)
);

CREATE INDEX IF NOT EXISTS tve_enquiry_followups_due_idx ON tve_enquiry_followups (status, due_at);

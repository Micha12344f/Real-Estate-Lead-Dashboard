-- One table per container: what one portal email was about, after parsing.
-- contact_id is the primary key (one row per contact, latest enquiry wins).
-- No FK: contacts lives in its own container; the backend keeps it true.

CREATE TABLE IF NOT EXISTS enquiries (
    contact_id BIGINT PRIMARY KEY,
    raw_email_body TEXT,
    property_reference TEXT,
    platform TEXT,
    move_in_date TEXT,
    duration TEXT,
    moving_with TEXT,
    pets TEXT,
    smokers TEXT,
    employment TEXT,
    affordability TEXT,
    adverse_credit TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
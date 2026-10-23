-- One table per container: the answers a contact gave in the questionnaire.
-- contact_id is the primary key: one completed questionnaire per contact.

CREATE TABLE IF NOT EXISTS questionnaire_responses (
    contact_id BIGINT PRIMARY KEY,
    move_timing TEXT,
    monthly_budget INTEGER,
    rental_duration TEXT,
    household_income INTEGER,
    income_proof TEXT,
    occupants INTEGER,
    pets TEXT,
    smoke_vape TEXT,
    adverse_credit TEXT,
    booking_opt_in TEXT,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
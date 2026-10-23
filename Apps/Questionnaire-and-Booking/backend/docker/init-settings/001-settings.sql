-- Encrypted credential vault: ciphertext only, never plaintext.
-- Fernet tokens are self-contained (version || iv || ct || hmac) so no separate
-- IV column is needed. `is_secret` lets the API render secret values as masked
-- in the snapshot endpoint. `updated_via` records the actor (e.g. "api", "env-bootstrap") for audit.

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    group_name TEXT NOT NULL,
    is_secret BOOLEAN NOT NULL,
    ciphertext BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_via TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS settings_group_idx ON settings(group_name);
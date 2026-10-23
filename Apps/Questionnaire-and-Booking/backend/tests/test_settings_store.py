"""Encrypted credential vault tests.

The vault talks to ``pg-settings`` through ``app.services.shared.database``. We
stub ``get_pool`` (no DB needed) and drive every code path with ``monkeypatch``
plus in-memory table state.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.shared import settings_store
from cryptography.fernet import Fernet


# --- Helpers -------------------------------------------------------------

class _FakeConn:
    """Mimics just enough of an asyncpg connection for the vault.

    ``rows`` is a dict keyed by ``key``; each value is the dict-shaped row
    the vault code expects (group_name, is_secret, updated_at, updated_via,
    ciphertext).
    """

    def __init__(self, rows: dict | None = None):
        self.rows: dict = rows or {}

    async def fetchrow(self, sql, key):
        return self.rows.get(key)

    async def fetchval(self, sql, key):
        row = self.rows.get(key)
        return None if row is None else row["ciphertext"]

    async def fetch(self, sql):
        return [dict(row) for row in self.rows.values()]

    async def execute(self, sql, key, *_args):
            # Distinguish DELETE (1 arg) from the upsert (5 args after key).
            if sql.lstrip().upper().startswith("DELETE"):
                if key in self.rows:
                    del self.rows[key]
                    return "DELETE 1"
                return "DELETE 0"
            if sql.lstrip().upper().startswith("INSERT"):
                # _UPSERT signature: (key, group, is_secret, ciphertext, updated_via)
                group_name, is_secret, ciphertext, updated_via = _args
                now = datetime.now(timezone.utc)
                self.rows[key] = {
                    "key": key,
                    "group_name": group_name,
                    "is_secret": is_secret,
                    "ciphertext": bytes(ciphertext),
                    "updated_at": now,
                    "updated_via": updated_via,
                }
                return "INSERT 0 1"
            raise AssertionError(f"unexpected SQL: {sql}")


class _FakePool:
    def __init__(self, rows: dict | None = None):
        self.conn = _FakeConn(rows or {})

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                return pool.conn

            async def __aexit__(self_inner, exc_type, exc, tb):
                return False

        return _Ctx()


@pytest.fixture
def fake_rows():
    return {}


@pytest.fixture
def master_key(monkeypatch):
    """Generate a fresh Fernet key and seed env + reset the cached Fernet."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("TVE_SETTINGS_MASTER_KEY", key)
    # Reset cached Fernet + cache so each test sees a fresh state.
    settings_store._fernet_instance = None
    settings_store._fernet_invalid_reason = None
    settings_store.invalidate_all()
    yield key
    settings_store.invalidate_all()
    settings_store._fernet_instance = None


@pytest.fixture
def pool_patch(fake_rows, monkeypatch):
    """Patch ``get_pool`` so the vault uses the in-memory rows."""
    pool = _FakePool(fake_rows)

    async def _fake_get_pool():
        return pool

    monkeypatch.setattr(settings_store, "_get_pool", _fake_get_pool)
    return pool


def _make_ciphertext(plaintext: str, key: bytes | str) -> bytes:
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key).encrypt(plaintext.encode("utf-8"))


# --- get -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_returns_env_when_db_row_missing(monkeypatch, pool_patch):
    monkeypatch.setenv("ACQUAINT_API_KEY", "env-key-abc")
    settings_store.invalidate_all()
    assert await settings_store.get("ACQUAINT_API_KEY") == "env-key-abc"


@pytest.mark.asyncio
async def test_get_returns_db_value_when_row_present(monkeypatch, master_key, pool_patch):
    cipher = _make_ciphertext("db-key-xyz", master_key)
    pool_patch.conn.rows["ACQUAINT_API_KEY"] = {
        "key": "ACQUAINT_API_KEY",
        "group_name": "acquaint",
        "is_secret": True,
        "ciphertext": cipher,
        "updated_at": datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc),
        "updated_via": "api",
    }
    # Env must be ignored when a DB row exists.
    monkeypatch.setenv("ACQUAINT_API_KEY", "env-key-should-be-ignored")
    settings_store.invalidate_all()
    assert await settings_store.get("ACQUAINT_API_KEY") == "db-key-xyz"


@pytest.mark.asyncio
async def test_get_returns_none_when_neither_db_nor_env(monkeypatch, pool_patch):
    monkeypatch.delenv("ACQUAINT_API_KEY", raising=False)
    settings_store.invalidate_all()
    assert await settings_store.get("ACQUAINT_API_KEY") is None


# --- set / roundtrip ----------------------------------------------------

@pytest.mark.asyncio
async def test_set_then_get_roundtrip(master_key, pool_patch):
    await settings_store.set(
        "ACQUAINT_API_KEY",
        value="round-trip-secret",
        group="acquaint",
        is_secret=True,
        updated_via="test",
    )
    # Ciphertext in the table is not equal to plaintext.
    stored = pool_patch.conn.rows["ACQUAINT_API_KEY"]["ciphertext"]
    assert stored != b"round-trip-secret"
    assert b"round-trip-secret" not in stored
    # Decryption roundtrip yields original.
    assert await settings_store.get("ACQUAINT_API_KEY") == "round-trip-secret"


@pytest.mark.asyncio
async def test_set_rejects_invalid_value(master_key, pool_patch):
    with pytest.raises(ValueError):
        await settings_store.set(
            "ACQUAINT_BASE",
            value="http://api.example.com",
            group="acquaint",
            is_secret=False,
            updated_via="test",
        )


@pytest.mark.asyncio
async def test_set_rejects_unknown_key(master_key, pool_patch):
    with pytest.raises(ValueError):
        await settings_store.set(
            "NOPE_KEY",
            value="x",
            group="acquaint",
            is_secret=False,
            updated_via="test",
        )


# --- delete -------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_removes_row_and_falls_back_to_env(monkeypatch, master_key, pool_patch):
    await settings_store.set(
        "ACQUAINT_API_KEY",
        value="stored-value",
        group="acquaint",
        is_secret=True,
        updated_via="test",
    )
    monkeypatch.setenv("ACQUAINT_API_KEY", "env-fallback")
    settings_store.invalidate_all()
    # Pre-condition: DB row shadows env.
    assert await settings_store.get("ACQUAINT_API_KEY") == "stored-value"
    deleted = await settings_store.delete("ACQUAINT_API_KEY")
    assert deleted is True
    # Falls back to env.
    assert await settings_store.get("ACQUAINT_API_KEY") == "env-fallback"


@pytest.mark.asyncio
async def test_delete_returns_false_when_no_row(master_key, pool_patch):
    deleted = await settings_store.delete("ACQUAINT_API_KEY")
    assert deleted is False


# --- master-key behaviour ----------------------------------------------

@pytest.mark.asyncio
async def test_write_with_no_master_key_raises_vault_locked(monkeypatch, pool_patch):
    monkeypatch.delenv("TVE_SETTINGS_MASTER_KEY", raising=False)
    settings_store._fernet_instance = None
    settings_store._fernet_invalid_reason = None
    with pytest.raises(RuntimeError) as exc:
        await settings_store.set(
            "ACQUAINT_API_KEY",
            value="something",
            group="acquaint",
            is_secret=True,
            updated_via="test",
        )
    assert str(exc.value) == "vault_locked"


@pytest.mark.asyncio
async def test_read_missing_row_with_no_master_key_falls_through_to_env(monkeypatch, pool_patch):
    monkeypatch.delenv("TVE_SETTINGS_MASTER_KEY", raising=False)
    settings_store._fernet_instance = None
    settings_store._fernet_invalid_reason = None
    monkeypatch.setenv("ACQUAINT_API_KEY", "env-still-works")
    settings_store.invalidate_all()
    assert await settings_store.get("ACQUAINT_API_KEY") == "env-still-works"


@pytest.mark.asyncio
async def test_read_existing_row_with_no_master_key_raises_vault_locked(monkeypatch, master_key, pool_patch):
    # Seed a row while the vault is unlocked.
    await settings_store.set(
        "ACQUAINT_API_KEY",
        value="stored-value",
        group="acquaint",
        is_secret=True,
        updated_via="test",
    )
    # Now drop the master key and try to read.
    monkeypatch.delenv("TVE_SETTINGS_MASTER_KEY", raising=False)
    settings_store._fernet_instance = None
    settings_store._fernet_invalid_reason = None
    settings_store.invalidate_all()
    with pytest.raises(RuntimeError) as exc:
        await settings_store.get("ACQUAINT_API_KEY")
    assert str(exc.value) == "vault_locked"


@pytest.mark.asyncio
async def test_invalid_master_key_raises_vault_locked(monkeypatch, pool_patch):
    monkeypatch.setenv("TVE_SETTINGS_MASTER_KEY", "not-a-real-fernet-key")
    settings_store._fernet_instance = None
    settings_store._fernet_invalid_reason = None
    with pytest.raises(RuntimeError) as exc:
        await settings_store.set(
            "ACQUAINT_API_KEY",
            value="x",
            group="acquaint",
            is_secret=True,
            updated_via="test",
        )
    assert str(exc.value) == "vault_locked"
    assert settings_store.vault_unlocked() is False


def test_vault_unlocked_reflects_state(monkeypatch):
    monkeypatch.delenv("TVE_SETTINGS_MASTER_KEY", raising=False)
    settings_store._fernet_instance = None
    settings_store._fernet_invalid_reason = None
    assert settings_store.vault_unlocked() is False


# --- snapshot ------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_secret_values_are_masked_not_plaintext(master_key, pool_patch):
    await settings_store.set(
        "ACQUAINT_API_KEY",
        value="supersecret1234",
        group="acquaint",
        is_secret=True,
        updated_via="test",
    )
    snap = await settings_store.snapshot()
    secret_entry = snap["ACQUAINT_API_KEY"]
    assert secret_entry["is_secret"] is True
    assert secret_entry["set"] is True
    # Last 4 chars of the secret.
    assert secret_entry["masked"] == "••••1234"
    # Plaintext must never appear in the snapshot for a secret.
    assert "value" not in secret_entry
    assert "supersecret1234" not in str(secret_entry)


@pytest.mark.asyncio
async def test_snapshot_non_secret_values_include_value(master_key, monkeypatch, pool_patch):
    monkeypatch.setenv("ACQUAINT_BASE", "https://api.example.com")
    settings_store.invalidate_all()
    snap = await settings_store.snapshot()
    entry = snap["ACQUAINT_BASE"]
    assert entry["is_secret"] is False
    assert entry["value"] == "https://api.example.com"
    assert "masked" not in entry


@pytest.mark.asyncio
async def test_snapshot_secret_no_row_has_no_mask(monkeypatch, pool_patch):
    monkeypatch.delenv("ACQUAINT_API_KEY", raising=False)
    snap = await settings_store.snapshot()
    assert snap["ACQUAINT_API_KEY"]["masked"] is None
    assert snap["ACQUAINT_API_KEY"]["set"] is False


# --- validate ------------------------------------------------------------

@pytest.mark.parametrize("key,value,expected_substring", [
    ("ACQUAINT_BASE", "http://api.example.com", "HTTPS"),
    ("ACQUAINT_BASE", "https://api.example.com/v1", "path"),
    ("ACQUAINT_SITE_PREFIX", "bad space", "match"),
    ("ACQUAINT_SITE_PREFIX", "x" * 65, "match"),
    ("TVE_IMAP_PORT", "99999", "between"),
    ("TVE_IMAP_PORT", "0", "between"),
    ("TVE_SMTP_PORT", "abc", "integer"),
    ("TVE_ENQUIRY_FROM", "not-an-email", "email"),
    ("ACQUAINT_API_KEY", "", "must not be empty"),
    ("ACQUAINT_API_KEY", "<set-a-real-key>", "placeholder"),
    ("ACQUAINT_API_KEY", "   ", "must not be empty"),
    ("TVE_AGENT_N8N_WEBHOOK_URL", "https://attacker.example/webhook/steal", "trusted Agent webhook endpoint"),
    ("TVE_AGENT_N8N_WEBHOOK_URL", "http://tve-n8n.hedgedge.info/webhook/tve-lead-intelligence-agent", "trusted Agent webhook endpoint"),
    ("TVE_SANDBOX_MODE", "true", "TRUE or FALSE"),
    ("TVE_SANDBOX_MODE", "paused", "TRUE or FALSE"),
])
def test_validate_rejects_bad_values(key, value, expected_substring):
    reason = settings_store.validate(key, value)
    assert reason is not None
    assert expected_substring.lower() in reason.lower()


@pytest.mark.parametrize("key,value", [
    ("ACQUAINT_BASE", "https://api.example.com"),
    ("ACQUAINT_SITE_PREFIX", "site_abc-123"),
    ("TVE_IMAP_PORT", "993"),
    ("TVE_SMTP_PORT", "587"),
    ("TVE_ENQUIRY_FROM", "lettings@example.com"),
    ("ACQUAINT_API_KEY", "real-secret-value"),
    ("TVE_AGENT_N8N_WEBHOOK_URL", "https://tve-n8n.hedgedge.info/webhook/tve-lead-intelligence-agent"),
    ("TVE_SANDBOX_MODE", "TRUE"),
    ("TVE_SANDBOX_MODE", "FALSE"),
])
def test_validate_accepts_good_values(key, value):
    assert settings_store.validate(key, value) is None


# --- TTL cache -----------------------------------------------------------

@pytest.mark.asyncio
async def test_ttl_cache_hits_within_window(master_key, pool_patch):
    cipher = _make_ciphertext("cached-value", master_key)
    pool_patch.conn.rows["ACQUAINT_API_KEY"] = {
        "key": "ACQUAINT_API_KEY",
        "group_name": "acquaint",
        "is_secret": True,
        "ciphertext": cipher,
        "updated_at": datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc),
        "updated_via": "api",
    }
    settings_store.invalidate_all()
    # First call seeds cache.
    assert await settings_store.get("ACQUAINT_API_KEY") == "cached-value"
    # Mutate the underlying "DB" without invalidating; cache should still win.
    pool_patch.conn.rows["ACQUAINT_API_KEY"]["ciphertext"] = _make_ciphertext("changed", master_key)
    assert await settings_store.get("ACQUAINT_API_KEY") == "cached-value"


@pytest.mark.asyncio
async def test_invalidate_clears_cache(master_key, pool_patch):
    cipher = _make_ciphertext("old-value", master_key)
    pool_patch.conn.rows["ACQUAINT_API_KEY"] = {
        "key": "ACQUAINT_API_KEY",
        "group_name": "acquaint",
        "is_secret": True,
        "ciphertext": cipher,
        "updated_at": datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc),
        "updated_via": "api",
    }
    settings_store.invalidate_all()
    assert await settings_store.get("ACQUAINT_API_KEY") == "old-value"
    pool_patch.conn.rows["ACQUAINT_API_KEY"]["ciphertext"] = _make_ciphertext("new-value", master_key)
    settings_store.invalidate("ACQUAINT_API_KEY")
    assert await settings_store.get("ACQUAINT_API_KEY") == "new-value"


@pytest.mark.asyncio
async def test_ttl_cache_does_not_cache_missing_keys(monkeypatch, master_key, pool_patch):
    # No DB row, no env → should not cache None, so a subsequent DB write
    # becomes visible immediately.
    monkeypatch.delenv("ACQUAINT_API_KEY", raising=False)
    settings_store.invalidate_all()
    assert await settings_store.get("ACQUAINT_API_KEY") is None
    cipher = _make_ciphertext("now-set", master_key)
    pool_patch.conn.rows["ACQUAINT_API_KEY"] = {
        "key": "ACQUAINT_API_KEY",
        "group_name": "acquaint",
        "is_secret": True,
        "ciphertext": cipher,
        "updated_at": datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc),
        "updated_via": "api",
    }
    assert await settings_store.get("ACQUAINT_API_KEY") == "now-set"


def test_ttl_is_clamped(monkeypatch):
    monkeypatch.setenv("TVE_SETTINGS_TTL_SECONDS", "99999")
    assert settings_store._ttl_seconds() == settings_store._MAX_TTL_SECONDS


# --- DB registration ----------------------------------------------------

def test_settings_dsn_registered():
    from app.services.shared import database
    assert database._DSNS["settings"] == "TVE_DB_SETTINGS"


@pytest.mark.asyncio
async def test_short_secret_mask_never_contains_the_full_value(master_key, pool_patch):
    await settings_store.set("ACQUAINT_API_KEY", value="abc", group="acquaint", is_secret=True, updated_via="test")
    assert (await settings_store.snapshot())["ACQUAINT_API_KEY"]["masked"] == "••••"


@pytest.mark.asyncio
async def test_cached_value_cannot_bypass_missing_or_changed_master_key(monkeypatch, master_key, pool_patch):
    await settings_store.set("ACQUAINT_API_KEY", value="cached-secret", group="acquaint", is_secret=True, updated_via="test")
    assert await settings_store.get("ACQUAINT_API_KEY") == "cached-secret"
    monkeypatch.delenv("TVE_SETTINGS_MASTER_KEY")
    with pytest.raises(RuntimeError, match="vault_locked"):
        await settings_store.get("ACQUAINT_API_KEY")
    monkeypatch.setenv("TVE_SETTINGS_MASTER_KEY", Fernet.generate_key().decode())
    with pytest.raises(RuntimeError, match="vault_decrypt_failed"):
        await settings_store.get("ACQUAINT_API_KEY")


@pytest.mark.asyncio
async def test_other_process_writes_are_observed_after_ttl(monkeypatch, master_key, pool_patch):
    clock = [100.0]
    monkeypatch.setattr(settings_store.time, "monotonic", lambda: clock[0])
    monkeypatch.setenv("TVE_SETTINGS_TTL_SECONDS", "5")
    await settings_store.set("ACQUAINT_API_KEY", value="first-secret", group="acquaint", is_secret=True, updated_via="test")
    assert await settings_store.get("ACQUAINT_API_KEY") == "first-secret"
    pool_patch.conn.rows["ACQUAINT_API_KEY"]["ciphertext"] = _make_ciphertext("second-secret", master_key)
    clock[0] += 6
    assert await settings_store.get("ACQUAINT_API_KEY") == "second-secret"

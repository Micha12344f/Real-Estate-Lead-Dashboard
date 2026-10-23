"""Isolation boundaries: no live fallback, cross-mode tokens or mixed operations."""
import asyncio
from types import SimpleNamespace

import asyncpg
import pytest
from cryptography.fernet import Fernet

from app.services.shared import config, database, settings_store, tokens_store
from app.services.questionnaire import store as legacy_store
from app.services.questionnaire.tokens import make_questionnaire_token, verified_guest_token
from app.workers import imap_intake
from test_settings_store import _FakePool


@pytest.fixture
def stores(monkeypatch):
    monkeypatch.setattr(database, "_pools", {})
    monkeypatch.setattr(database, "_lock", asyncio.Lock())
    for name, env in database._DSNS.items():
        monkeypatch.setenv(env, f"postgresql://live:password@pg-{name}:5432/{name}")
        monkeypatch.setenv(env.replace("TVE_DB_", "TVE_SANDBOX_DB_"),
                           f"postgresql://test:other-password@pg-sandbox:5432/sandbox_{name}")
    created = []

    async def create_pool(dsn, **kwargs):
        pool = _FakePool()
        pool.dsn = dsn
        created.append(pool)
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    return created


@pytest.mark.asyncio
async def test_all_stores_reselect_on_toggle_and_never_reuse_live_pools(stores):
    config.set_runtime_mode(False)
    live = {name: await database.get_pool(name) for name in database._DSNS}
    config.set_runtime_mode(True)
    sandbox = {name: await database.get_pool(name) for name in database._DSNS}
    assert len(stores) == 10
    assert all("@pg-sandbox:" in pool.dsn for pool in sandbox.values())
    assert all(sandbox[name] is not live[name] for name in live)
    config.set_runtime_mode(False)
    assert all([await database.get_pool(name) is live[name] for name in live])


@pytest.mark.asyncio
async def test_missing_sandbox_dsn_fails_even_with_cached_pool(stores, monkeypatch):
    config.set_runtime_mode(True)
    await database.get_pool("contacts")
    monkeypatch.delenv("TVE_SANDBOX_DB_CONTACTS")
    with pytest.raises(database.SandboxStorageError, match="not_configured"):
        await database.get_pool("contacts")
    assert len(stores) == 1


@pytest.mark.parametrize("dsn", [
    "postgresql://different-user:different-password@pg-contacts:5432/contacts",
    "postgresql://different-user@PG-CONTACTS/contacts",
    "postgresql://different-user@pg-responses:5432/responses",
    "postgresql://different-user@pg-contacts:5432/%63ontacts",
])
def test_sandbox_rejects_production_database_even_with_other_credentials(stores, monkeypatch, dsn):
    monkeypatch.setenv("TVE_SANDBOX_DB_CONTACTS", dsn)
    with pytest.raises(database.SandboxStorageError, match="matches_production"):
        database.selected_dsn("contacts", True)


@pytest.mark.parametrize("query", ["host=pg-contacts", "database=contacts", "dbname=contacts", "port=5432"])
def test_sandbox_rejects_query_overrides_of_database_identity(stores, monkeypatch, query):
    monkeypatch.setenv("TVE_SANDBOX_DB_CONTACTS", "postgresql://test@pg-sandbox/sandbox_contacts?" + query)
    with pytest.raises(database.SandboxStorageError, match="invalid_sandbox"):
        database.selected_dsn("contacts", True)


@pytest.mark.asyncio
async def test_pinned_operation_survives_toggle_across_await_and_thread(stores):
    config.set_runtime_mode(True)
    with config.mode_scope(True):
        before = await database.get_pool("contacts")
        config.set_runtime_mode(False)
        await asyncio.sleep(0)
        assert await asyncio.to_thread(config.is_sandbox) is True
        assert await database.get_pool("contacts") is before
        assert "sandbox_tokens" in (await database.get_pool("tokens")).dsn
        assert config.journey_url("booking").startswith("http://127.0.0.1:3000/")
    assert config.is_sandbox() is False
    assert "@pg-contacts:" in (await database.get_pool("contacts")).dsn


@pytest.mark.asyncio
async def test_settings_cache_and_env_are_isolated_but_live_crm_is_readonly(stores, monkeypatch):
    monkeypatch.setenv("TVE_SETTINGS_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("TVE_TOKEN_KEY", "live-signing-secret")
    monkeypatch.setenv("TVE_SMTP_PASSWORD", "live-mail-secret")
    monkeypatch.setenv("TVE_SANDBOX_TOKEN_KEY", "test-signing-secret")
    settings_store.invalidate_all()
    try:
        config.set_runtime_mode(False)
        await settings_store.set("TVE_TOKEN_KEY", value="vault-live-key", group="security", is_secret=True, updated_via="test")
        assert await settings_store.get("TVE_TOKEN_KEY") == "vault-live-key"
        await settings_store.set("ACQUAINT_API_KEY", value="live-read-key", group="acquaint", is_secret=True, updated_via="test")
        config.set_runtime_mode(True)
        assert await settings_store.get("TVE_TOKEN_KEY") == "test-signing-secret"
        assert await settings_store.get("TVE_SMTP_PASSWORD") is None
        await settings_store.set("TVE_TOKEN_KEY", value="vault-test-key", group="security", is_secret=True, updated_via="test")
        assert await settings_store.get("TVE_TOKEN_KEY") == "vault-test-key"
        assert await settings_store.get("ACQUAINT_API_KEY") == "live-read-key"
        with pytest.raises(RuntimeError, match="readonly"):
            await settings_store.set("ACQUAINT_API_KEY", value="accidental-write", group="acquaint", is_secret=True, updated_via="test")
        metadata = await settings_store.snapshot()
        assert metadata["ACQUAINT_API_KEY"]["readonly"] is True
        assert metadata["TVE_TOKEN_KEY"]["environment"] == "sandbox"
        config.set_runtime_mode(False)
        assert await settings_store.get("TVE_TOKEN_KEY") == "vault-live-key"
    finally:
        settings_store.invalidate_all()


@pytest.mark.asyncio
async def test_same_secret_cannot_verify_tokens_in_other_mode(monkeypatch):
    async def get(key):
        return "identical-secret"
    async def no_pool(*args, **kwargs):
        raise AssertionError("Cross-mode tokens must fail before database access")
    monkeypatch.setattr(settings_store, "get", get)
    monkeypatch.setattr(tokens_store, "get_pool", no_pool)
    config.set_runtime_mode(False)
    live_key = await tokens_store.signing_key()
    live = tokens_store._signed_token("booking", live_key)
    live_guest = make_questionnaire_token("demo@sandbox.invalid", "Test", 242, live_key)
    config.set_runtime_mode(True)
    assert await tokens_store.verify_token(live, "booking") is None
    assert await verified_guest_token(live_guest) is None
    assert await tokens_store.verify_token("old-unsigned-production-token", "booking") is None
    test_key = await tokens_store.signing_key()
    test = tokens_store._signed_token("booking", test_key)
    test_guest = make_questionnaire_token("demo@sandbox.invalid", "Test", 242, test_key)
    assert await verified_guest_token(test_guest) is not None
    config.set_runtime_mode(False)
    assert await tokens_store.verify_token(test, "booking") is None
    assert await verified_guest_token(test_guest) is None


@pytest.mark.asyncio
async def test_guest_submission_uses_sandbox_responses_not_legacy_live_dsn(monkeypatch):
    config.set_runtime_mode(True)
    calls = []
    class Connection:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def fetchrow(self, sql, *args):
            calls.append(args)
            return {"id": 123}
    async def pool(name):
        assert name == "responses"
        return SimpleNamespace(acquire=Connection)
    async def live_connection(*args, **kwargs):
        raise AssertionError("Sandbox must not connect to legacy production database")
    monkeypatch.setattr(database, "get_pool", pool)
    monkeypatch.setattr(asyncpg, "connect", live_connection)
    monkeypatch.setattr(legacy_store, "DB_DSN", "postgresql://live:secret@legacy/tve")
    saved = await legacy_store.save_submission("guest", "test-token", {"occupants": 2}, "test@sandbox.invalid")
    assert saved == {"id": 123, "stored": True}
    assert calls[0][2] == "test@sandbox.invalid"


@pytest.mark.asyncio
async def test_missing_sandbox_storage_pauses_mailpit_and_followups(monkeypatch):
    config.set_runtime_mode(True)
    async def missing(*args, **kwargs):
        raise database.SandboxStorageError("sandbox_database_not_configured")
    async def side_effect(*args, **kwargs):
        raise AssertionError("Missing storage must pause all sandbox effects")
    monkeypatch.setattr(database, "get_pool", missing)
    monkeypatch.setattr(imap_intake, "poll_mailpit", side_effect)
    monkeypatch.setattr(imap_intake, "deliver_due_followups", side_effect)
    await imap_intake.run_batch()

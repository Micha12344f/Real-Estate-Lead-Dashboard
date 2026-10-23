import pytest

from app.services.shared import acquaint, config, runtime_mode, settings_store
from app.services.enquiries import followup
from app.workers import imap_intake


@pytest.mark.asyncio
async def test_database_mode_refresh_controls_sync_services_and_mailpit(monkeypatch):
    monkeypatch.setenv("sandbox", "FALSE")
    monkeypatch.delenv("TVE_SANDBOX_MODE", raising=False)
    config.set_runtime_mode(None)
    mode = {"value": "TRUE"}
    credentials = {"TVE_IMAP_HOST": "imap.example.test", "TVE_IMAP_USERNAME": "staff",
                   "TVE_IMAP_PASSWORD": "password"}

    async def get(key):
        return mode["value"] if key == "TVE_SANDBOX_MODE" else None

    async def get_many(group):
        return credentials if group == "imap" else {}

    monkeypatch.setattr(settings_store, "get", get)
    monkeypatch.setattr(settings_store, "get_many", get_many)
    assert await runtime_mode.refresh() is True
    assert config.is_sandbox() is True
    assert imap_intake.inbound_configuration() is None
    assert acquaint.client.is_mock is True
    assert (await followup.mail_configuration())["host"] == followup.MOCK_INBOX_SMTP_HOST

    mode["value"] = "FALSE"
    assert await runtime_mode.refresh() is False
    assert config.is_sandbox() is False
    assert (await imap_intake._imap_config())["host"] == "imap.example.test"
    assert acquaint.client.is_mock is False


@pytest.mark.asyncio
async def test_runtime_mode_retains_last_known_value_on_store_error(monkeypatch):
    config.set_runtime_mode(True, "dashboard")

    async def unavailable(_key):
        raise RuntimeError("vault_locked")

    monkeypatch.setattr(settings_store, "get", unavailable)
    assert await runtime_mode.refresh() is True
    assert config.is_sandbox() is True
    config.set_runtime_mode(None)

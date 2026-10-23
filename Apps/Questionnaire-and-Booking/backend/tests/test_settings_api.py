"""Vault API boundary: auth, ciphertext, write-only responses, audit, and probes."""
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import main
from app.routes.dashboard import settings as routes
from app.services.shared import settings_store
from app.services.shared import config
from test_settings_store import _FakePool


@pytest.fixture
def vault(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("TVE_SETTINGS_MASTER_KEY", key)
    monkeypatch.setenv("TVE_WEBHOOK_SECRET", "bootstrap-webhook")
    monkeypatch.setenv("TVE_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("ACQUAINT_BASE", "https://api.example.test")
    monkeypatch.setenv("ACQUAINT_API_KEY", "bootstrap-api-secret")
    monkeypatch.setenv("ACQUAINT_SITE_PREFIX", "TEST")
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    monkeypatch.setattr(main, "TVE_WEBHOOK_SECRET", "bootstrap-webhook")
    pool = _FakePool()

    async def get_pool():
        return pool

    monkeypatch.setattr(settings_store, "_get_pool", get_pool)
    monkeypatch.setenv("sandbox", "FALSE")
    config.set_runtime_mode(None)
    settings_store.invalidate_all()
    config.set_runtime_mode(None)
    with TestClient(main.app) as client:
        yield client, pool, key
    settings_store.invalidate_all()


AUTH = {"X-TVE-Auth": "bootstrap-webhook"}


def test_settings_require_auth_on_every_operation(vault):
    client, _, _ = vault
    for method, path, body in [("GET", "", None), ("PUT", "/ACQUAINT_API_KEY", {"value": "secret"}),
                               ("DELETE", "/ACQUAINT_API_KEY", None), ("POST", "/test/smtp", {}),
                               ("GET", "/mode", None), ("PUT", "/mode", {"sandbox": True})]:
        assert client.request(method, "/dashboard/settings" + path, json=body).status_code == 401


def test_roundtrip_is_encrypted_and_write_only_in_responses_and_audit(vault):
    client, pool, master = vault
    secret = "unique-api-secret-1234"
    response = client.put("/dashboard/settings/ACQUAINT_API_KEY", json={"value": secret}, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["masked"] == "••••1234"
    assert "value" not in response.json()
    stored = pool.conn.rows["ACQUAINT_API_KEY"]["ciphertext"]
    assert secret.encode() not in stored
    assert Fernet(master.encode()).decrypt(stored).decode() == secret
    metadata = client.get("/dashboard/settings", headers=AUTH)
    assert metadata.status_code == 200
    assert len(metadata.json()["groups"]) == 5
    agent_fields = next(group["fields"] for group in metadata.json()["groups"] if group["id"] == "agent")
    assert len(agent_fields) == 3
    dedicated_secret = next(field for field in agent_fields if field["key"] == "TVE_AGENT_N8N_WEBHOOK_SECRET")
    assert dedicated_secret["is_secret"] is True
    assert "value" not in dedicated_secret
    assert secret not in metadata.text
    assert "bootstrap-api-secret" not in metadata.text
    events = client.get("/debug/events", headers=AUTH)
    assert secret not in events.text
    saved = [event for event in events.json()["events"] if event["event"] == "settings_saved"][-1]
    assert saved["setting_key"] == "ACQUAINT_API_KEY"
    assert saved["setting_group"] == "acquaint"
    cleared = client.delete("/dashboard/settings/ACQUAINT_API_KEY", headers=AUTH)
    assert cleared.json()["source"] == "environment"
    assert cleared.json()["set"] is True
    assert "ACQUAINT_API_KEY" not in pool.conn.rows


@pytest.mark.parametrize("key,value", [
    ("ACQUAINT_BASE", "http://api.example.test"),
    ("ACQUAINT_BASE", "https://user:secret@api.example.test"),
    ("ACQUAINT_BASE", "https://api.example.test:99999"),
    ("TVE_AGENT_N8N_WEBHOOK_URL", "https://attacker.example/webhook/steal"),
    ("TVE_AGENT_N8N_WEBHOOK_URL", "http://tve-n8n.hedgedge.info/webhook/tve-lead-intelligence-agent"),
    ("ACQUAINT_SITE_PREFIX", "bad prefix"),
    ("TVE_SMTP_PORT", "65536"),
    ("TVE_IMAP_PORT", "zero"),
    ("TVE_ENQUIRY_FROM", "bad@example.test\r\nBcc: victim@example.test"),
    ("ACQUAINT_API_KEY", "<set-key>"),
    ("ACQUAINT_API_KEY", 123),
])
def test_invalid_values_are_rejected_without_echoing_input(vault, key, value):
    client, pool, _ = vault
    response = client.put("/dashboard/settings/" + key, json={"value": value}, headers=AUTH)
    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "requestId"}
    assert str(value) not in body["error"]["message"]
    assert str(value) not in body["error"]["code"]
    assert key not in pool.conn.rows


def test_scope_is_credentials_only(vault):
    client, _, _ = vault
    for key in ("sandbox", "TVE_DB_CONTACTS", "TVE_SETTINGS_MASTER_KEY"):
        assert client.put("/dashboard/settings/" + key, json={"value": "not-exposed"}, headers=AUTH).status_code == 404


def test_mode_toggle_updates_effective_runtime_mode_and_is_audited(vault, monkeypatch):
    from app.services.shared import database
    async def ready():
        pass
    monkeypatch.setattr(database, "verify_sandbox_storage", ready)
    client, pool, _ = vault
    current = client.get("/dashboard/settings/mode", headers=AUTH)
    assert current.status_code == 200
    assert current.json() == {"sandbox": False, "mode": "production", "source": "environment", "environmentMode": "production"}

    changed = client.put("/dashboard/settings/mode", json={"sandbox": True}, headers=AUTH)
    assert changed.status_code == 200
    assert changed.json()["sandbox"] is True
    assert changed.json()["mode"] == "sandbox"
    assert changed.json()["source"] == "dashboard"
    assert config.is_sandbox() is True
    assert pool.conn.rows["TVE_SANDBOX_MODE"]["group_name"] == "mode"

    back = client.put("/dashboard/settings/mode", json={"sandbox": False}, headers=AUTH)
    assert back.status_code == 200
    assert config.is_sandbox() is False
    events = client.get("/debug/events", headers=AUTH).json()["events"]
    assert any(event["event"] == "mode_changed" and event["setting_group"] == "mode" for event in events)


def test_mode_toggle_preserves_production_when_sandbox_storage_is_missing(vault):
    client, pool, _ = vault
    response = client.put("/dashboard/settings/mode", json={"sandbox": True}, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "sandbox_storage_unavailable"
    assert "TVE_SANDBOX_MODE" not in pool.conn.rows
    assert config.is_sandbox() is False


def test_stale_dashboard_environment_is_rejected_before_action(vault):
    client, _, _ = vault
    response = client.post("/dashboard/actions/assign-property", json={"contact_id": 7, "property_id": 242},
                           headers={**AUTH, "X-TVE-Mode": "sandbox"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mode_changed"
    assert response.headers["X-TVE-Mode"] == "production"


def test_test_data_loader_rejects_production(vault):
    client, _, _ = vault
    assert client.post("/dashboard/settings/sandbox/seed", headers=AUTH).status_code == 409


def test_mode_toggle_rejects_non_boolean_and_fails_closed_with_locked_vault(vault, monkeypatch):
    client, _, _ = vault
    assert client.put("/dashboard/settings/mode", json={"sandbox": "TRUE"}, headers=AUTH).status_code == 422
    monkeypatch.delenv("TVE_SETTINGS_MASTER_KEY")
    response = client.put("/dashboard/settings/mode", json={"sandbox": True}, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "vault_locked"
    assert config.is_sandbox() is False


def test_locked_vault_has_metadata_and_blocks_save_and_clear(vault, monkeypatch):
    client, pool, _ = vault
    client.put("/dashboard/settings/ACQUAINT_API_KEY", json={"value": "stored-secret"}, headers=AUTH)
    monkeypatch.delenv("TVE_SETTINGS_MASTER_KEY")
    snapshot = client.get("/dashboard/settings", headers=AUTH)
    assert snapshot.status_code == 200
    assert snapshot.json()["vault_unlocked"] is False
    assert "stored-secret" not in snapshot.text
    for method in ("PUT", "DELETE"):
        response = client.request(method, "/dashboard/settings/ACQUAINT_API_KEY", json={"value": "replacement"}, headers=AUTH)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "vault_locked"
    assert "ACQUAINT_API_KEY" in pool.conn.rows


def test_webhook_rotation_accepts_bootstrap_and_current_and_rejects_previous(vault):
    client, _, _ = vault
    new = "new-webhook-secret-5678"
    assert client.put("/dashboard/settings/TVE_WEBHOOK_SECRET", json={"value": new}, headers=AUTH).status_code == 200
    assert client.get("/dashboard/settings", headers={"X-TVE-Auth": new}).status_code == 200
    assert client.get("/dashboard/settings", headers=AUTH).status_code == 200
    assert client.put("/dashboard/settings/TVE_WEBHOOK_SECRET", json={"value": "next-webhook-secret"}, headers=AUTH).status_code == 200
    assert client.get("/dashboard/settings", headers={"X-TVE-Auth": new}).status_code == 401
    assert client.get("/dashboard/settings", headers={"X-TVE-Auth": "next-webhook-secret"}).status_code == 200


def test_connection_candidates_are_tested_without_persisting(vault, monkeypatch):
    client, pool, _ = vault
    seen = []

    async def probe(values):
        seen.append(values)

    monkeypatch.setattr(routes, "_acquaint_probe", probe)
    response = client.post("/dashboard/settings/test/acquaint", headers=AUTH,
                           json={"values": {"ACQUAINT_API_KEY": "unsaved-secret"}})
    assert response.json()["ok"] is True
    assert seen[0]["ACQUAINT_API_KEY"] == "unsaved-secret"
    assert pool.conn.rows == {}
    assert "unsaved-secret" not in response.text
    assert client.post("/dashboard/settings/test/acquaint", headers=AUTH,
                       json={"values": {"TVE_SMTP_HOST": "wrong-group"}}).status_code == 422


def test_probe_errors_never_echo_credentials(vault, monkeypatch):
    client, _, _ = vault

    def probe(values):
        raise RuntimeError("echoed-server-password-should-not-leak")

    monkeypatch.setattr(routes, "_smtp_probe", probe)
    response = client.post("/dashboard/settings/test/smtp", headers=AUTH, json={})
    assert response.json()["ok"] is False
    assert "echoed-server-password" not in response.text
    assert "echoed-server-password" not in client.get("/debug/events", headers=AUTH).text


def test_smtp_probe_only_uses_helo_and_never_sends_or_authenticates(monkeypatch):
    calls = []

    class SMTP:
        def __init__(self, host, port, timeout):
            calls.append((host, port, timeout))

        def ehlo(self):
            calls.append("ehlo")
            return 250, b"ok"

        def close(self):
            calls.append("close")

    monkeypatch.setattr(routes.smtplib, "SMTP", SMTP)
    routes._smtp_probe({"TVE_SMTP_HOST": "smtp.example.test", "TVE_SMTP_PORT": "1025"})
    assert calls == [("smtp.example.test", 1025, 5), "ehlo", "close"]


def test_imap_probe_selects_readonly_and_closes(monkeypatch):
    calls = []

    class IMAP:
        def __init__(self, host, port, ssl_context, timeout):
            calls.append((host, port, timeout))

        def login(self, username, password):
            calls.append("login")

        def select(self, mailbox, readonly):
            calls.append((mailbox, readonly))
            return "OK", []

        def shutdown(self):
            calls.append("close")

    monkeypatch.setattr(routes.imaplib, "IMAP4_SSL", IMAP)
    routes._imap_probe({"TVE_IMAP_HOST": "imap.example.test", "TVE_IMAP_USERNAME": "user", "TVE_IMAP_PASSWORD": "secret"})
    assert calls == [("imap.example.test", 993, 5), "login", ("INBOX", True), "close"]

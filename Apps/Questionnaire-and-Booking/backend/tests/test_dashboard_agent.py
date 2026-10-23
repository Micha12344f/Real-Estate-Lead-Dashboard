from fastapi.testclient import TestClient
import pytest

from app import main
from app.routes.dashboard import agent


def test_agent_chat_rejects_non_allowlisted_model(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    with TestClient(main.app) as client:
        response = client.post("/dashboard/agent/chat", json={
            "message": "hello", "sessionId": "session", "history": [], "model": "cloud-model"
        })
    assert response.status_code == 422


def test_agent_chat_fails_closed_without_dedicated_secret(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    monkeypatch.setenv("TVE_WEBHOOK_SECRET", "dashboard-proxy-secret")
    monkeypatch.delenv("TVE_AGENT_N8N_WEBHOOK_SECRET", raising=False)

    async def setting(key):
        return {"TVE_AGENT_N8N_WEBHOOK_URL": agent.DEFAULT_URL}.get(key)

    monkeypatch.setattr(agent.settings_store, "get", setting)
    with TestClient(main.app) as client:
        response = client.post("/dashboard/agent/chat", json={
            "message": "hello", "sessionId": "session", "history": [], "model": "qwen3.5:27b"
        })
    assert response.status_code == 503
    assert "Agent webhook secret" in response.json()["error"]["message"]


def test_agent_chat_uses_dedicated_secret_only_and_never_returns_it(monkeypatch):
    secret = "n8n-agent-header-secret-for-test"
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    monkeypatch.setenv("TVE_WEBHOOK_SECRET", "different-dashboard-auth")

    async def setting(key):
        return {"TVE_AGENT_N8N_WEBHOOK_URL": agent.DEFAULT_URL,
                "TVE_AGENT_N8N_WEBHOOK_SECRET": secret}.get(key)

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"reply": "Hello", "toolCalls": [{"name": "read", "summary": "Fetched data"}]}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, *, json, headers):
            assert url == agent.DEFAULT_URL
            assert headers["X-TVE-Auth"] == secret
            assert headers["X-TVE-Auth"] != "different-dashboard-auth"
            assert set(json) == {"message", "sessionId", "history", "model"}
            return Response()

    monkeypatch.setattr(agent.settings_store, "get", setting)
    monkeypatch.setattr(agent.httpx, "AsyncClient", Client)
    with TestClient(main.app) as client:
        response = client.post("/dashboard/agent/chat", json={
            "message": "hello", "sessionId": "session", "history": [], "model": "qwen3.5:27b"
        })
    assert response.status_code == 200
    assert response.json()["reply"] == "Hello"
    assert secret not in response.text


@pytest.mark.parametrize("url", [
    "https://attacker.example/webhook/steal",
    "http://tve-n8n.hedgedge.info/webhook/tve-lead-intelligence-agent",
])
def test_agent_chat_rejects_untrusted_webhook_url_before_network(monkeypatch, url):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)

    async def setting(key):
        return {"TVE_AGENT_N8N_WEBHOOK_URL": url,
                "TVE_AGENT_N8N_WEBHOOK_SECRET": "test-agent-secret"}.get(key)

    def forbidden_client(**kwargs):
        raise AssertionError("Untrusted webhook destination must not open a network client")

    monkeypatch.setattr(agent.settings_store, "get", setting)
    monkeypatch.setattr(agent.httpx, "AsyncClient", forbidden_client)
    with TestClient(main.app) as client:
        response = client.post("/dashboard/agent/chat", json={
            "message": "hello", "sessionId": "session", "history": [], "model": "qwen3.5:27b"
        })
    assert response.status_code == 503
    assert url not in response.text


def test_property_contacts_deduplicates_tenancy_links(monkeypatch):
    class CRM:
        async def list_tenancies(self, property_id):
            assert property_id == 12
            return [{"contactId": 7, "relationshipTypeId": 2, "startDate": "2025-01-01", "status": "Current"},
                    {"contactId": 7, "relationshipTypeId": 2}]

        async def get_contact(self, contact_id):
            assert contact_id == 7
            return {"firstName": "Ada", "lastName": "Lovelace", "email": "ada@example.test", "phone": "not exposed"}

    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    monkeypatch.setattr(agent.acquaint, "client", CRM())
    with TestClient(main.app) as client:
        response = client.get("/dashboard/properties/12/contacts")
    assert response.status_code == 200
    assert response.json()["contacts"] == [{
        "contactId": 7, "name": "Ada Lovelace", "email": "ada@example.test",
        "relationshipTypeId": 2, "startDate": "2025-01-01", "endDate": None, "status": "Current"
    }]

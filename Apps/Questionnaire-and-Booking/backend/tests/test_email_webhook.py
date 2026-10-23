"""Webhook email intake route: raw RFC822 and base64 JSON bodies."""
import base64
from email.message import EmailMessage
from email.policy import SMTP

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import app
import app.routes.shared.intake as intake_route

client = TestClient(app)
PATH = "/webhook/thames-valley-estates/email-intake"


def _sample() -> bytes:
    message = EmailMessage()
    message["From"] = "Rightmove <autoresponder@rightmove.com>"
    message["Subject"] = "Lettings enquiry: 2 High Street"
    message["Message-ID"] = "<webhook-1@example.test>"
    message.set_content(
        "Hi, Jane has enquired about this property\n\n2 High Street, Reading, RG1 1AA\n\u00a31,250 pcm\n1 bed\n\n"
        "Jane Applicant\njane@example.test\n07700 900123\nAPPLICANT WOULD LIKE\n    Book a viewing\n")
    return message.as_bytes(policy=SMTP)


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    calls = {}

    async def fake_ingest(pool, raw, **kwargs):
        calls["raw"] = raw
        calls["kwargs"] = kwargs
        return 42

    async def fake_followups(pool, *, limit=10):
        calls["followups"] = True
        return 1

    monkeypatch.setattr(intake_route, "ingest_email", fake_ingest)
    monkeypatch.setattr(intake_route, "deliver_due_followups", fake_followups)

    async def fake_pool():
        return object()

    monkeypatch.setattr(intake_route, "get_pool", fake_pool)
    return calls


def test_email_intake_raw_rfc822(patched):
    response = client.post(PATH, content=_sample(), headers={"Content-Type": "message/rfc822"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted" and body["contactId"] == 42
    assert body["isEnquiry"] is True and body["invitationsSent"] == 1
    assert patched["raw"] == _sample()


def test_email_intake_base64_json(patched):
    encoded = base64.b64encode(_sample()).decode()
    response = client.post(PATH, json={"raw": encoded})
    assert response.status_code == 200
    assert response.json()["contactId"] == 42
    assert patched["raw"] == _sample()


def test_email_intake_rejects_bad_payloads(patched):
    # Empty body
    assert client.post(PATH, content=b"", headers={"Content-Type": "message/rfc822"}).status_code == 422
    # JSON without a raw field
    assert client.post(PATH, json={"foo": "bar"}).status_code == 422
    # Invalid base64
    assert client.post(PATH, json={"raw": "not*base64"}).status_code == 422
    # A message with no From header
    message = EmailMessage()
    message["Subject"] = "no sender"
    message.set_content("hi")
    response = client.post(PATH, content=message.as_bytes(), headers={"Content-Type": "message/rfc822"})
    assert response.status_code == 422


def test_email_intake_non_enquiry_returns_no_contact(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)

    async def fake_ingest(pool, raw, **kwargs):
        return None

    async def fake_pool():
        return object()

    monkeypatch.setattr(intake_route, "ingest_email", fake_ingest)
    monkeypatch.setattr(intake_route, "get_pool", fake_pool)
    response = client.post(PATH, content=_sample(), headers={"Content-Type": "message/rfc822"})
    assert response.status_code == 200
    assert response.json()["contactId"] is None
    assert response.json()["isEnquiry"] is False
    assert response.json()["invitationsSent"] == 0


def test_email_intake_requires_auth(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    monkeypatch.setattr(main, "TVE_WEBHOOK_SECRET", "test-secret")
    response = client.post(PATH, content=_sample(), headers={"Content-Type": "message/rfc822"})
    assert response.status_code == 401

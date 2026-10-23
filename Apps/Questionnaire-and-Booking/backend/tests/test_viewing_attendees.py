"""Acquaint Viewing requires an actual CRM contact in attendees."""
import json

import httpx
import pytest

from app.services.shared.acquaint import AcquaintReadOnlyClient, AcquaintContactError, SandboxAcquaintClient


LEAD = {"id": 7, "first_name": "Test", "last_name": "Applicant",
        "email": "applicant@example.test", "phone": "07000000000"}


def crm(handler):
    def transport(request):
        if request.url.path == "/v1/auth":
            return httpx.Response(200, json={"token": "test-token"})
        return handler(request)
    return AcquaintReadOnlyClient("https://crm.example.test", "key", "TVES", transport=httpx.MockTransport(transport))


@pytest.mark.asyncio
async def test_existing_exact_email_is_used_without_contact_creation():
    def handler(request):
        assert request.method == "GET"
        assert request.url.params["requestContacts.email"] == LEAD["email"]
        return httpx.Response(200, json={"data": [
            {"id": 123, "email": "another@example.test"},
            {"id": 456, "email": "APPLICANT@example.test"},
        ]})
    assert await crm(handler).ensure_viewing_contact(LEAD) == 456


@pytest.mark.asyncio
async def test_missing_applicant_is_created_and_then_reused_on_retry():
    contacts, writes = [], []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": contacts})
        assert request.url.path == "/v1/Contacts/LettingsApplicant"
        body = json.loads(request.content)
        writes.append(body)
        contacts.append({"id": 456, "email": body["email"]})
        return httpx.Response(200, json={"id": 456})
    client = crm(handler)
    assert await client.ensure_viewing_contact(LEAD) == 456
    assert await client.ensure_viewing_contact(LEAD) == 456
    assert len(writes) == 1
    assert writes[0]["firstNames"] == "Test"
    assert writes[0]["mobile"] == LEAD["phone"]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"data": [{"id": 1, "email": LEAD["email"]}, {"id": 2, "email": LEAD["email"]}]},
    {"unexpected": []},
])
async def test_ambiguous_or_invalid_lookup_never_creates_contact(response):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json=response)
    with pytest.raises(AcquaintContactError):
        await crm(handler).ensure_viewing_contact(LEAD)


@pytest.mark.asyncio
async def test_sandbox_attendee_never_uses_live_write():
    def handler(request):
        pytest.fail("sandbox must not call Acquaint to create an attendee")
    sandbox = SandboxAcquaintClient(live_client=crm(handler))
    assert await sandbox.ensure_viewing_contact(LEAD) == LEAD["id"]

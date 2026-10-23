"""Production applicant journey through real services with in-memory DB/HTTP/SMTP.

No provider requests or delivered messages: HTTP uses MockTransport and SMTP
constructors are replaced, while routing, signing, persistence SQL, slot rules,
mail selection and dashboard stitching remain the application implementations.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.routes.booking.confirm as confirm_route
import app.routes.questionnaire.submissions as submissions_route
from app.services.booking import availability
from app.services.dashboard import readmodel
from app.services.enquiries import followup, journey
from app.services.questionnaire import store as legacy_store
from app.services.questionnaire.tokens import make_questionnaire_token
from app.services.shared import (acquaint, contacts, database, enquiries_store,
                                  responses_store, settings_store, tokens_store)
from app.services.shared import contact_write_guard

PREFIX = "/webhook/thames-valley-estates/"
KEY = "production-test-signing-key"
ANSWERS = {
    "move_timing": "Within 2 weeks", "monthly_budget": 1600,
    "rental_duration": "Over 1 year", "household_income": 60000,
    "income_proof": "Yes", "occupants": 2, "pets": "No",
    "smoke_vape": "No", "adverse_credit": "No", "booking_opt_in": "opt_in",
}


class MemoryDB:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.contact = {"id": 7, "first_name": "Jane", "last_name": "Applicant",
                        "email": "jane@example.test", "phone": None,
                        "status": "Questionnaire_sent", "acquaint_property_id": 242,
                        "viewing_slot_start": None, "viewing_slot_end": None,
                        "created_at": now, "updated_at": now}
        self.tokens = {}
        self.answers = None
        self.effects = []
        self.lock = asyncio.Lock()
        self.add_token("questionnaire")

    def add_token(self, purpose):
        row = {"id": len(self.tokens) + 1, "contact_id": 7, "purpose": purpose,
               "token": tokens_store._signed_token(purpose, KEY),
               "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
               "consumed_at": None, "created_at": datetime.now(timezone.utc)}
        self.tokens[purpose] = row
        return row["token"]

    async def get_pool(self, name="contacts"):
        assert name in ("contacts", "responses", "tokens", "enquiries")
        return MemoryConnection(self, name)


class MemoryConnection:
    def __init__(self, db, name):
        self.db, self.name = db, name

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        async with self.db.lock:
            yield

    async def fetchrow(self, sql, *args):
        sql = " ".join(sql.split())
        db = self.db
        if self.name == "contacts":
            assert sql.startswith(("SELECT * FROM contacts WHERE id=$1", "SELECT id FROM contacts WHERE id=$1"))
            return dict(db.contact) if args[0] == db.contact["id"] else None
        if self.name == "responses":
            if sql.startswith("SELECT"):
                return dict(db.answers) if db.answers else None
            assert sql.startswith("INSERT INTO questionnaire_responses")
            fields = ("contact_id", "move_timing", "monthly_budget", "rental_duration",
                      "household_income", "income_proof", "occupants", "pets",
                      "smoke_vape", "adverse_credit", "booking_opt_in")
            db.answers = dict(zip(fields, args), completed_at=datetime.now(timezone.utc))
            db.effects.append("answers")
            return {"inserted": True}
        if self.name == "enquiries":
            return None
        if sql.startswith("SELECT token"):
            return db.tokens.get(args[1])
        if sql.startswith("SELECT id, contact_id"):
            row = db.tokens.get(args[1])
            return dict(row) if row and row["token"] == args[0] else None
        if sql.startswith("INSERT INTO tokens"):
            contact_id, purpose, token, expires_at = args
            assert contact_id == 7
            if purpose in db.tokens:
                return None
            db.add_token(purpose)
            db.tokens[purpose]["token"] = token
            db.tokens[purpose]["expires_at"] = expires_at
            db.effects.append("booking_token")
            return db.tokens[purpose]
        if sql.startswith("UPDATE tokens"):
            contact_id, token, expires_at, purpose = args
            row = db.tokens[purpose]
            row.update(token=token, expires_at=expires_at, consumed_at=None)
            db.effects.append("refresh_token")
            return row
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        db = self.db
        if sql.startswith("SELECT pg_advisory"):
            assert args == (242,)
            return
        if sql.startswith("UPDATE tokens"):
            row = next(row for row in db.tokens.values() if row["id"] == args[0])
            row["consumed_at"] = datetime.now(timezone.utc)
            db.effects.append("consume:" + row["purpose"])
            return
        if "viewing_slot_start=$3" in sql:
            id_, property_id, start, end = args
            db.contact.update(acquaint_property_id=property_id, viewing_slot_start=start,
                              viewing_slot_end=end, status="viewing_requested")
            db.effects.append("viewing")
            return
        if "status='questionnaire_completed'" in sql:
            db.contact["status"] = "questionnaire_completed"
            db.effects.append("completed")
            return
        if "ARRAY_POSITION" in sql:
            id_, stage, stages = args
            if stages.index(db.contact["status"]) < stages.index(stage):
                db.contact["status"] = stage
            return
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if sql == "SELECT 1 FROM lead_delete_operations WHERE contact_id=$1":
            return None
        if sql == "SELECT contact_id FROM tokens WHERE id=$1":
            return next((row["contact_id"] for row in self.db.tokens.values() if row["id"] == args[0]), None)
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        if self.name == "tokens":
            return list(self.db.tokens.values())
        assert "viewing_slot_start IS NOT NULL" in sql
        row = self.db.contact
        return [dict(row)] if row["viewing_slot_start"] and row["acquaint_property_id"] == args[0] else []


@pytest.fixture
def production(monkeypatch):
    monkeypatch.delenv("SANDBOX", raising=False)
    monkeypatch.setenv("sandbox", "FALSE")
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    values = {"TVE_TOKEN_KEY": KEY, "ACQUAINT_BASE": "https://crm.example.test",
              "ACQUAINT_API_KEY": "test-key", "ACQUAINT_SITE_PREFIX": "TVES",
              "TVE_SMTP_HOST": "smtp.example.test", "TVE_SMTP_PORT": "587",
              "TVE_SMTP_USER": "smtp-user", "TVE_SMTP_PASSWORD": "smtp-password",
              "TVE_ENQUIRY_FROM": "lettings@example.test"}

    async def get(key):
        return values.get(key)

    async def get_many(group):
        assert group in ("smtp", "acquaint")
        return {key: value for key, value in values.items()
                if settings_store._SCOPE[key]["group"] == group}

    monkeypatch.setattr(settings_store, "get", get)
    monkeypatch.setattr(settings_store, "get_many", get_many)
    db = MemoryDB()
    for module in (database, contacts, tokens_store, responses_store, enquiries_store,
                   submissions_route, confirm_route, contact_write_guard):
        monkeypatch.setattr(module, "get_pool", db.get_pool)

    provider_calls, mail, smtp_calls = [], [], []
    catalogue = [{"id": "242", "statusId": 1, "street": "42 Real Catalogue Road",
                  "town": "Reading", "postcode": "RG1 1AA", "price": 1400}]
    appointments = []

    def provider(request):
        provider_calls.append(request)
        if request.url.path == "/v1/auth":
            return httpx.Response(200, json={"token": "mock-auth", "expiresIn": 3600})
        assert request.method == "GET", "CRM mutations forbidden"
        if request.url.path == "/v1/Properties/list":
            return httpx.Response(200, json={"data": catalogue})
        assert request.url.path == "/v1/Appointments/list"
        assert request.url.params["request.propertyId"] == "242"
        return httpx.Response(200, json=appointments)

    crm = acquaint.make_client(transport=httpx.MockTransport(provider))
    monkeypatch.setattr(acquaint, "client", crm)
    monkeypatch.setattr(availability, "acquaint", crm)
    monkeypatch.setattr(journey, "fixture_properties", lambda: pytest.fail("Production used fixtures"))
    monkeypatch.setattr(journey, "BOOKING_URL", "https://thamesvalley-estate-agents-booking.vercel.app/booking")
    monkeypatch.setattr(followup, "BOOKING_URL", journey.BOOKING_URL)

    class SMTP:
        def __init__(self, host, port, **kwargs):
            smtp_calls.append((host, port))
            assert host == "smtp.example.test"
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ehlo(self): smtp_calls.append("ehlo")
        def starttls(self, **kwargs): smtp_calls.append("tls")
        def login(self, user, password):
            assert (user, password) == ("smtp-user", "smtp-password")
            smtp_calls.append("login")
        def send_message(self, message): mail.append(message)

    monkeypatch.setattr(followup.smtplib, "SMTP", SMTP)
    monkeypatch.setattr(followup.smtplib, "SMTP_SSL", SMTP)
    legacy = []

    async def legacy_save(**kwargs):
        legacy.append(kwargs)
        return {"id": 90}

    monkeypatch.setattr(legacy_store, "save_submission", legacy_save)
    with TestClient(main.app) as client:
        yield SimpleNamespace(db=db, client=client, values=values, mail=mail,
                              smtp_calls=smtp_calls, provider_calls=provider_calls,
                              catalogue=catalogue, appointments=appointments, legacy=legacy)


def submit(p, token=None, answers=None):
    return p.client.post(PREFIX + "questionnaire-submit", json={
        "token": token if token is not None else p.db.tokens["questionnaire"]["token"],
        "answers": ANSWERS if answers is None else answers, "consent": True})


@pytest.mark.parametrize("port", [587, 465])
def test_production_submission_availability_viewing_and_dashboard(production, port):
    p = production
    p.values["TVE_SMTP_PORT"] = str(port)
    submitted = submit(p)
    assert submitted.status_code == 200
    assert submitted.json()["localMock"] is False
    link = submitted.json()["bookingUrl"]
    assert link.startswith(journey.BOOKING_URL + "/")
    token = link.rsplit("/", 1)[1]
    assert tokens_store._valid_signature(token, "booking", KEY)
    assert p.db.answers["monthly_budget"] == 1600
    assert p.db.contact["status"] == "booking_link_sent"
    assert p.db.tokens["questionnaire"]["consumed_at"]
    assert len(p.mail) == 1 and link in p.mail[0].get_content()
    offered = p.client.get(PREFIX + "booking-availability", params={"token": token})
    assert offered.status_code == 200
    data = offered.json()
    assert data["localMock"] is False
    assert data["property"]["id"] == 242  # not the catalogue's string or a fixture ID
    assert data["property"]["address"] == "42 Real Catalogue Road, Reading, RG1 1AA"
    slot = data["slots"][0]
    body = {"token": token, "propertyId": 242, "slotStart": slot["start"],
            "email": "attacker@example.test", "contactId": 999}
    saved = p.client.post(PREFIX + "booking-confirm", json=body)
    assert saved.status_code == 200
    ack = saved.json()
    assert ack["status"] == "acknowledged" and ack["localMock"] is False
    assert "not confirmed yet" in ack["message"]
    assert "appointmentId" not in ack and ack["requestId"] == 7
    assert ack["slot"] == {"start": slot["start"], "end": slot["end"]}
    assert p.db.contact["status"] == "viewing_requested"
    assert p.db.tokens["booking"]["consumed_at"]
    assert len(p.mail) == 2
    assert all(message["To"] == "jane@example.test" for message in p.mail)
    assert "appointment is not confirmed yet" in p.mail[1].get_content()
    assert ("smtp.example.test", port) in p.smtp_calls and "login" in p.smtp_calls
    assert ("tls" in p.smtp_calls) == (port == 587)
    assert p.client.post(PREFIX + "booking-confirm", json=body).json() == ack
    assert submit(p, answers={**ANSWERS, "monthly_budget": 999}).json() == submitted.json()
    assert p.db.answers["monthly_budget"] == 1600 and len(p.mail) == 2
    reopened = p.client.get(PREFIX + "booking-availability", params={"token": token}).json()
    assert reopened["booked"] == ack and reopened["slots"] == []
    other = {**body, "slotStart": data["slots"][1]["start"]}
    assert p.client.post(PREFIX + "booking-confirm", json=other).status_code == 409
    assert p.db.effects.count("viewing") == p.db.effects.count("answers") == 1
    dashboard = p.client.get("/dashboard/leads/7")
    assert dashboard.status_code == 200
    # Also exercise the service contract directly, independent of route envelope.
    lead = asyncio.run(readmodel.lead(7))
    assert lead["status"] == "viewing_requested"
    assert lead["questionnaire"]["monthly_budget"] == 1600
    assert lead["viewing_slot_start"] == slot["start"]
    assert lead["links"]["booking"] == link
    assert p.legacy == []


@pytest.mark.parametrize("endpoint", ["questionnaire-submit", "booking-availability", "booking-confirm"])
@pytest.mark.parametrize("failure", ["expired", "wrong-purpose", "tampered", "unknown", "empty"])
def test_production_rejects_tokens_before_effects(production, endpoint, failure):
    p = production
    purpose = "questionnaire" if endpoint == "questionnaire-submit" else "booking"
    token = p.db.tokens["questionnaire"]["token"] if purpose == "questionnaire" else p.db.add_token("booking")
    if failure == "expired":
        p.db.tokens[purpose]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif failure == "wrong-purpose":
        other = "booking" if purpose == "questionnaire" else "questionnaire"
        token = p.db.tokens.get(other, {}).get("token") or p.db.add_token(other)
    elif failure == "tampered":
        token = ("b" if token[0] == "a" else "a") + token[1:]
    elif failure == "unknown":
        token = tokens_store._signed_token(purpose, KEY)
    else:
        token = ""
    if endpoint == "questionnaire-submit":
        response = submit(p, token)
    elif endpoint == "booking-availability":
        response = p.client.get(PREFIX + endpoint, params={"token": token, "propertyId": 242})
    else:
        response = p.client.post(PREFIX + endpoint, json={"token": token, "propertyId": 242, "slotStart": "2099-01-01T10:00:00Z"})
    assert response.status_code == 401
    assert p.db.effects == [] and p.mail == [] and p.provider_calls == [] and p.legacy == []


@pytest.mark.parametrize("linked", [242, None])
def test_production_property_override_is_rejected_before_provider_or_effects(production, linked):
    p = production
    p.db.contact.update(status="questionnaire_completed", acquaint_property_id=linked)
    token = p.db.add_token("booking")
    assert p.client.get(PREFIX + "booking-availability", params={"token": token, "propertyId": 999}).status_code == 403
    assert p.client.post(PREFIX + "booking-confirm", json={"token": token, "propertyId": 999, "slotStart": "2099-01-01T10:00:00Z"}).status_code == 403
    assert p.db.effects == [] and p.provider_calls == [] and p.mail == []


@pytest.mark.parametrize("stage", ["enquiried", "Questionnaire_sent", "questionnaire_completed"])
def test_staff_booking_override_works_without_qualification(production, stage):
    p = production
    p.db.contact["status"] = stage
    sent = p.client.post("/dashboard/actions/send-booking", json={"contact_id": 7})
    assert sent.status_code == 200
    assert sent.json()["sent"] is True
    assert p.db.contact["status"] == "booking_link_sent"
    assert p.db.answers is None
    token = p.db.tokens["booking"]["token"]
    assert token in p.mail[0].get_content()
    offered = p.client.get(PREFIX + "booking-availability", params={"token": token})
    assert offered.status_code == 200
    slot = offered.json()["slots"][0]
    booked = p.client.post(PREFIX + "booking-confirm", json={"token": token, "slotStart": slot["start"]})
    assert booked.status_code == 200
    assert p.db.contact["status"] == "viewing_requested"
    assert p.db.answers is None


def test_production_catalogue_gates(production):
    p = production
    token = p.db.add_token("booking")
    p.catalogue[:] = [{"id": 10001, "sourcePropertyId": 242, "statusId": 1}]
    assert p.client.get(PREFIX + "booking-availability", params={"token": token}).status_code == 409
    p.catalogue[:] = [{"id": 242, "statusId": 2}]
    assert p.client.post(PREFIX + "booking-confirm", json={"token": token, "slotStart": "2099-01-01T10:00:00Z"}).status_code == 409
    assert p.db.effects == [] and p.mail == []


def test_questionnaire_after_staff_invitation_keeps_sent_stage_and_no_second_email(production):
    p = production
    sent = p.client.post("/dashboard/actions/send-booking", json={"contact_id": 7})
    assert sent.status_code == 200
    assert p.db.contact["status"] == "booking_link_sent"
    submitted = submit(p)
    assert submitted.status_code == 200
    assert p.db.answers["monthly_budget"] == ANSWERS["monthly_budget"]
    assert p.db.contact["status"] == "booking_link_sent"
    assert len(p.mail) == 1
    assert submit(p).status_code == 200
    assert len(p.mail) == 1


def test_production_slots_block_provider_appointments_and_saved_requests(production):
    p = production
    slots = asyncio.run(availability.build_slots(242))
    p.appointments[:] = [{"startDate": slots[0]["start"], "endDate": slots[0]["end"]}]
    p.db.contact.update(viewing_slot_start=datetime.fromisoformat(slots[1]["start"]),
                        viewing_slot_end=datetime.fromisoformat(slots[1]["end"]))
    remaining = asyncio.run(availability.build_slots(242))
    assert slots[0] not in remaining and slots[1] not in remaining and slots[2] in remaining


def test_production_slot_recheck_rejects_unoffered_time(production):
    p = production
    p.db.contact["status"] = "questionnaire_completed"
    token = p.db.add_token("booking")
    response = p.client.post(PREFIX + "booking-confirm", json={"token": token, "slotStart": "2099-01-01T10:00:00Z"})
    assert response.status_code == 409
    assert p.db.effects == [] and p.mail == []


def test_production_expired_booking_link_is_replaced_on_submission(production):
    p = production
    old = p.db.add_token("booking")
    p.db.tokens["booking"]["expires_at"] = datetime.now(timezone.utc) - timedelta(days=1)
    response = submit(p)
    token = response.json()["bookingUrl"].rsplit("/", 1)[1]
    assert token != old
    assert asyncio.run(tokens_store.verify_token(token, "booking"))
    assert asyncio.run(tokens_store.verify_token(old, "booking")) is None


def test_production_verified_legacy_guest_compatibility_and_binding(production):
    p = production
    token = make_questionnaire_token("guest@example.test", "Guest", 242, KEY)
    response = submit(p, token)
    assert response.status_code == 200 and response.json()["contactEmail"] == "guest@example.test"
    assert p.legacy[0]["contact_email"] == "guest@example.test" and p.db.effects == []
    assert p.client.get(PREFIX + "booking-availability", params={"token": token}).status_code == 200
    assert p.client.get(PREFIX + "booking-availability", params={"token": token, "propertyId": 999}).status_code == 403
    assert p.client.post(PREFIX + "booking-confirm", json={"token": token, "propertyId": 999, "slotStart": "2099-01-01T10:00:00Z"}).status_code == 403
    bad = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert submit(p, bad).status_code == 401
    assert len(p.legacy) == 1 and p.mail == []


def test_sandbox_response_still_describes_simulation(production, monkeypatch):
    monkeypatch.setenv("sandbox", "TRUE")
    row = dict(production.db.contact, viewing_slot_start=datetime.now(timezone.utc),
               viewing_slot_end=datetime.now(timezone.utc) + timedelta(minutes=30))
    response = journey.contact_viewing_response(row)
    assert response["localMock"] is True and "simulated" in response["message"]


@pytest.mark.asyncio
async def test_production_concurrent_retries_save_and_send_once(production):
    p = production
    pool = await p.db.get_pool()
    token = p.db.tokens["questionnaire"]["token"]
    results = await asyncio.gather(*(journey.submit_questionnaire(pool, token, ANSWERS, True) for _ in range(2)))
    assert results[0] == results[1]
    booking = results[0]["bookingUrl"].rsplit("/", 1)[1]
    slot = (await availability.build_slots(242))[0]
    results = await asyncio.gather(*(journey.store_viewing(pool, booking, datetime.fromisoformat(slot["start"]), 242) for _ in range(2)))
    assert results[0] == results[1]
    assert p.db.effects.count("answers") == p.db.effects.count("viewing") == 1
    assert len(p.mail) == 2


@pytest.mark.parametrize("legacy", ["a" * 43, "sbx_" + "a" * 64])
def test_production_existing_opaque_contact_links_use_database_identity(production, monkeypatch, legacy):
    p = production
    # Compatibility requires the unchanged bootstrap key and an exact stored row.
    monkeypatch.setenv("TVE_TOKEN_KEY", KEY)
    p.db.tokens["questionnaire"]["token"] = legacy
    assert submit(p, legacy).status_code == 200
    assert p.db.answers and p.legacy == [] and len(p.mail) == 1


def test_production_unknown_contact_is_rejected_before_effects(production):
    p = production
    p.db.contact["id"] = 8
    assert submit(p).status_code == 401
    assert p.db.effects == [] and p.mail == [] and p.legacy == []


# --- Score gate: only a score above 80 unlocks the booking link ---

@pytest.mark.parametrize("expected_score,overrides", [
    (79, {"move_timing": "Within 3 weeks", "adverse_credit": "Yes, settled"}),
    (80, {"move_timing": "After a month", "smoke_vape": "Yes"}),
    (81, {"move_timing": "Within 4 weeks", "monthly_budget": 999}),
])
def test_production_booking_score_threshold_and_identical_retry(production, expected_score, overrides):
    p = production
    answers = {**ANSWERS, **overrides}
    journey.validate_answers(answers, True)
    assert asyncio.run(journey.booking_score(answers, p.db.contact)) == expected_score
    response = submit(p, answers=answers)
    assert response.status_code == 200
    body = response.json()
    retry = submit(p, answers=answers)
    assert retry.status_code == 200 and retry.json() == body
    unlocked = expected_score > 80
    assert body["bookingUnlocked"] is unlocked
    assert bool(body["bookingUrl"]) is unlocked
    if not unlocked:
        assert body["bookingUrl"] is None
    assert ("booking" in p.db.tokens) is unlocked
    assert len(p.mail) == int(unlocked)
    assert p.db.contact["status"] == ("booking_link_sent" if unlocked else "questionnaire_completed")
    assert p.db.effects.count("answers") == 1


LOW_SCORE_ANSWERS = {
    "move_timing": "Within 4 weeks", "monthly_budget": 900,
    "rental_duration": "1 - 3 months", "household_income": 20000,
    "income_proof": "No", "occupants": 1, "pets": "Yes - multiple",
    "smoke_vape": "Yes", "adverse_credit": "Yes, active", "booking_opt_in": "opt_out",
}


def test_production_high_score_gets_booking_link(production):
    p = production
    response = submit(p)  # ANSWERS score 100 > 80
    assert response.status_code == 200
    body = response.json()
    assert body["bookingUnlocked"] is True
    assert body["bookingUrl"] and body["bookingUrl"].startswith(journey.BOOKING_URL + "/")
    assert p.db.contact["status"] == "booking_link_sent"
    assert len(p.mail) == 1  # booking link emailed


def test_production_low_score_receives_no_booking_link(production):
    p = production
    response = submit(p, answers=LOW_SCORE_ANSWERS)  # score <= 80
    assert response.status_code == 200
    body = response.json()
    assert body["bookingUnlocked"] is False
    assert body["bookingUrl"] is None
    # Questionnaire still recorded and completed; no booking token, no email.
    assert p.db.answers is not None
    assert p.db.contact["status"] == "questionnaire_completed"
    assert "booking" not in p.db.tokens
    assert p.mail == []
    # The applicant cannot reach availability without a booking token.
    assert p.client.get(PREFIX + "booking-availability",
                        params={"token": "none"}).status_code == 401


def test_production_low_score_retry_is_consistent_and_silent(production):
    p = production
    pool = asyncio.run(p.db.get_pool())
    token = p.db.tokens["questionnaire"]["token"]
    first = asyncio.run(journey.submit_questionnaire(pool, token, LOW_SCORE_ANSWERS, True))
    second = asyncio.run(journey.submit_questionnaire(pool, token, LOW_SCORE_ANSWERS, True))
    assert first == second
    assert first["bookingUnlocked"] is False and first["bookingUrl"] is None
    assert p.mail == [] and "booking" not in p.db.tokens
    assert p.db.effects.count("answers") == 1

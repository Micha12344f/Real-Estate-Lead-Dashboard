"""Exercise qualifying and gated contacts against a fresh simulation stack."""
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "backend"))
from app.services.enquiries.parser import parse_email
from app.services.enquiries.simulator import scenario_message

DASH = "http://127.0.0.1:3002/sandbox/api/"
SUBMIT_URL = "http://127.0.0.1:3001/questionnaire/api/questionnaire-submit"
QUALIFYING_FIXTURE_INDEX = 0
LOW_SCORE_FIXTURE_INDEX = 1
# These Rightmove fixtures resolve to source property 222 (£2,187 pcm).
QUALIFYING_ANSWERS = {
    "move_timing": "Within 1 week", "monthly_budget": 3000, "rental_duration": "Over 1 year",
    "household_income": 120000, "income_proof": "Yes", "occupants": 2, "pets": "No",
    "smoke_vape": "No", "adverse_credit": "No", "booking_opt_in": "opt_in",
}
LOW_SCORE_ANSWERS = {
    **QUALIFYING_ANSWERS, "move_timing": "Within 4 weeks", "monthly_budget": 1500,
    "household_income": 50000, "income_proof": "No",
}


def request(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)


def wait_contact(email, predicate):
    for _ in range(60):
        contact = next((c for c in request(DASH + "runs")["runs"] if c["email"] == email), None)
        if contact and predicate(contact): return contact
        time.sleep(1)
    raise AssertionError("Contact did not reach expected journey stage")


def start_contact(index):
    existing = {c["email"] for c in request(DASH + "runs")["runs"]}
    parsed = parse_email(scenario_message("rightmove", index, uuid.UUID(int=0), "<e2e@example.invalid>").as_bytes())
    assert parsed.sender_email and not parsed.needs_review, f"Rightmove fixture {index} must be a complete enquiry"
    assert parsed.sender_email not in existing, f"Rightmove fixture {index} already exists; use a fresh isolated test stack"
    sent = request(DASH + "runs", {"scenario": "rightmove", "fixtureIndex": index})
    assert sent["contactEmail"] == parsed.sender_email
    contact = wait_contact(sent["contactEmail"], lambda c: c["status"] == "Questionnaire_sent" and "questionnaire" in c["links"])
    assert contact["acquaint_property_id"]
    return contact


def submit_answers(contact, answers, *, expected_score, booking_unlocked):
    q_token = urlsplit(contact["links"]["questionnaire"]).path.rsplit("/", 1)[1]
    body = {"payload": {"token": q_token, "answers": answers, "consent": True}}
    submitted = request(SUBMIT_URL, body)
    assert submitted["submissionId"] == contact["id"]
    assert request(SUBMIT_URL, body) == submitted, "Identical questionnaire retry changed its result"
    detail = request(DASH + f"leads/{contact['id']}")
    score = detail["score"]["value"]
    assert score == expected_score, f"Fixture score drifted: expected {expected_score}, got {score}; rent={detail['property_rent']}"
    assert (score > 80) is booking_unlocked
    assert submitted["bookingUnlocked"] is booking_unlocked
    expected_status = "booking_link_sent" if booking_unlocked else "questionnaire_completed"
    assert detail["status"] == expected_status
    assert {key: detail["questionnaire"][key] for key in answers} == answers
    assert detail["tokens"]["questionnaire"]["consumed_at"]
    if booking_unlocked:
        assert isinstance(submitted["bookingUrl"], str) and submitted["bookingUrl"]
        assert detail["links"]["booking"] == submitted["bookingUrl"]
    else:
        assert submitted["bookingUrl"] is None
        assert detail["links"]["booking"] is None and detail["tokens"]["booking"] is None
        assert detail["viewing_slot_start"] is None and detail["viewing_slot_end"] is None
    return submitted


def mail_subjects(email):
    messages = request("http://127.0.0.1:8025/api/v1/messages?limit=100")["messages"]
    return [m["Subject"] for m in messages if any(t["Address"] == email for t in m["To"])]


def main():
    assert request(DASH + "scenarios")["sandbox"] is True
    contact = start_contact(QUALIFYING_FIXTURE_INDEX)
    contact_id = contact["id"]
    submitted = submit_answers(contact, QUALIFYING_ANSWERS, expected_score=100, booking_unlocked=True)
    b_token = urlsplit(submitted["bookingUrl"]).path.rsplit("/", 1)[1]
    availability_url = "http://127.0.0.1:3000/booking/api/availability?token=" + b_token
    availability = request(availability_url)
    assert availability["slots"] and not availability["booked"]
    confirm_url = "http://127.0.0.1:3000/booking/api/confirm"
    confirm = {"token": b_token, "propertyId": availability["property"]["id"], "slotStart": availability["slots"][0]["start"]}
    booked = request(confirm_url, confirm)
    assert booked["requestId"] == request(confirm_url, confirm)["requestId"] == contact_id
    assert request(availability_url)["booked"]["slot"]["start"] == booked["slot"]["start"]
    if len(availability["slots"]) > 1:
        try:
            request(confirm_url, {**confirm, "slotStart": availability["slots"][1]["start"]})
            raise AssertionError("Second viewing should be rejected")
        except urllib.error.HTTPError as error:
            assert error.code == 409
            assert json.load(error)["error"]["booked"]["requestId"] == contact_id
    final = wait_contact(contact["email"], lambda c: c["status"] == "viewing_requested")
    assert final["viewing_slot_start"] and final["viewing_slot_end"]
    subjects = mail_subjects(contact["email"])
    for subject in ("Next step: your property enquiry | Thames Valley Estates",
                    "Choose a viewing time | Thames Valley Estates",
                    "We’ve received your viewing request | Thames Valley Estates"):
        assert subjects.count(subject) == 1

    gated = start_contact(LOW_SCORE_FIXTURE_INDEX)
    assert gated["id"] != contact_id
    submit_answers(gated, LOW_SCORE_ANSWERS, expected_score=31, booking_unlocked=False)
    gated_final = wait_contact(gated["email"], lambda c: c["status"] == "questionnaire_completed")
    assert "booking" not in gated_final["links"]
    q_token = urlsplit(gated["links"]["questionnaire"]).path.rsplit("/", 1)[1]
    try:
        request("http://127.0.0.1:3000/booking/api/availability?token=" + q_token)
    except urllib.error.HTTPError as error:
        assert error.code == 401
    else:
        raise AssertionError("Gated applicant's questionnaire token unlocked booking availability")
    assert mail_subjects(gated["email"]) == ["Next step: your property enquiry | Thames Valley Estates"]
    report = {"status":"passed","contactId":contact_id,"cases":[
        {"name":"qualifying","fixtureIndex":QUALIFYING_FIXTURE_INDEX,"contactId":contact_id,"score":100},
        {"name":"low-score","fixtureIndex":LOW_SCORE_FIXTURE_INDEX,"contactId":gated["id"],"score":31}],
        "checks":["qualifying journey","idempotent questionnaire and booking","score-gated completion",
                  "no gated booking token/link/email","questionnaire token cannot book"]}
    Path(__file__).with_name("last-e2e-result.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report,indent=2))


if __name__ == "__main__": main()

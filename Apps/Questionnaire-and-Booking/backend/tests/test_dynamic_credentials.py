"""Runtime rotation at consumer boundaries, without provider services or Postgres."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import httpx
import pytest
from fastapi import HTTPException

import app.routes.booking.guest as guest_route
import app.routes.dashboard.actions as actions_route
import app.routes.questionnaire.submissions as submissions_route
from app.services.enquiries import followup
from app.services.questionnaire import store as submission_store
from app.services.questionnaire.tokens import verify_questionnaire_token
from app.services.shared import acquaint, contacts, database, settings_store, tokens_store
from app.workers import imap_intake


@pytest.fixture
def credentials(monkeypatch):
    values = {
        "ACQUAINT_BASE": "https://old-crm.example.test",
        "ACQUAINT_API_KEY": "old-api-key",
        "ACQUAINT_SITE_PREFIX": "OLD",
        "TVE_SMTP_HOST": "old-smtp.example.test", "TVE_SMTP_USER": "old-user",
        "TVE_SMTP_PASSWORD": "old-password", "TVE_ENQUIRY_FROM": "old@example.test",
        "TVE_IMAP_HOST": "old-imap.example.test", "TVE_IMAP_USERNAME": "old-user",
        "TVE_IMAP_PASSWORD": "old-password",
        "TVE_TOKEN_KEY": "old-signing-key",
    }

    async def get(key):
        return values.get(key)

    async def get_many(group):
        return {key: value for key, value in values.items()
                if settings_store._SCOPE[key]["group"] == group}

    monkeypatch.setattr(settings_store, "get", get)
    monkeypatch.setattr(settings_store, "get_many", get_many)
    monkeypatch.delenv("SANDBOX", raising=False)
    monkeypatch.setenv("sandbox", "FALSE")
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox", [False, True])
@pytest.mark.parametrize("key,new_value", [
    ("ACQUAINT_BASE", "https://new-crm.example.test"),
    ("ACQUAINT_API_KEY", "new-api-key"),
    ("ACQUAINT_SITE_PREFIX", "NEW"),
])
async def test_acquaint_rebuilds_on_each_credential_change_and_keeps_auth_cache(
        monkeypatch, credentials, sandbox, key, new_value):
    monkeypatch.setenv("sandbox", str(sandbox))
    calls = []
    auth_count = 0

    def handle(request):
        nonlocal auth_count
        calls.append(request)
        if request.url.path == "/v1/auth":
            auth_count += 1
            assert request.url.host == httpx.URL(credentials["ACQUAINT_BASE"]).host
            assert json.loads(request.content) == {
                "apiKey": credentials["ACQUAINT_API_KEY"], "sitePrefix": credentials["ACQUAINT_SITE_PREFIX"]}
            return httpx.Response(200, json={"token": f"auth-{auth_count}", "expiresIn": 3600})
        assert request.headers["Authorization"] == f"Bearer auth-{auth_count}"
        return httpx.Response(200, json={"data": [{"id": auth_count, "statusId": 0}]})

    client = acquaint.make_client(transport=httpx.MockTransport(handle))
    assert await client.list_properties(0) == [{"id": 1, "statusId": 0}]
    first = client._live if sandbox else client._configured
    assert await client.list_properties(0) == [{"id": 1, "statusId": 0}]
    assert auth_count == 1
    assert (client._live if sandbox else client._configured) is first

    credentials[key] = new_value
    assert await client.list_properties(0) == [{"id": 2, "statusId": 0}]
    assert (client._live if sandbox else client._configured) is not first
    assert auth_count == 2
    assert calls[-1].url.host == httpx.URL(credentials["ACQUAINT_BASE"]).host


@pytest.mark.asyncio
async def test_sandbox_removal_drops_live_catalogue_and_can_recover(monkeypatch, credentials):
    monkeypatch.setenv("sandbox", "TRUE")
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/v1/auth":
            return httpx.Response(200, json={"token": "auth"})
        return httpx.Response(200, json={"data": [{"id": 42}]})

    client = acquaint.make_client(transport=httpx.MockTransport(handle))
    assert await client.list_properties(0) == [{"id": 42}]
    del credentials["ACQUAINT_API_KEY"]
    assert await client.list_properties() == acquaint.fixture_properties()
    assert await client.list_appointments(10001) == []
    assert not client.is_live
    assert len(requests) == 2
    credentials["ACQUAINT_API_KEY"] = "restored-key"
    assert await client.list_properties(0) == [{"id": 42}]
    assert len(requests) == 4


@pytest.mark.asyncio
async def test_production_removal_fails_closed_without_reusing_old_auth(credentials):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"token": "auth"} if request.url.path == "/v1/auth" else [])

    client = acquaint.make_client(transport=httpx.MockTransport(handle))
    await client.list_appointments(42)
    del credentials["ACQUAINT_API_KEY"]
    with pytest.raises(acquaint.AcquaintConfigurationError):
        await client.list_appointments(42)
    assert len(requests) == 2
    credentials["ACQUAINT_API_KEY"] = "restored-key"
    await client.list_appointments(42)
    assert len(requests) == 4


@pytest.mark.asyncio
async def test_acquaint_rejects_mutations_before_reading_settings(monkeypatch, credentials):
    async def forbidden(group):
        pytest.fail("Settings read for blocked request")

    monkeypatch.setattr(settings_store, "get_many", forbidden)
    client = acquaint.make_client()
    with pytest.raises(acquaint.AcquaintReadOnlyError):
        await client.request("POST", acquaint.READ_ENDPOINTS["appointments"])
    with pytest.raises(acquaint.AcquaintReadOnlyError):
        await client.get("/v1/not-allowlisted")


@pytest.mark.asyncio
async def test_journey_email_uses_rotated_smtp_and_sender_per_send(monkeypatch, credentials):
    sent = []
    async def advance(*args):
        pass
    monkeypatch.setattr(followup.contacts_store, "advance_status", advance)
    monkeypatch.setattr(followup, "_smtp_send", lambda config, message: sent.append((config, message)))
    identity = {"id": 7, "first_name": "Jane", "email": "jane@example.test"}
    await followup.send_journey_email(identity, "booking_link", token="token")
    credentials.update({"TVE_SMTP_HOST": "new-smtp.example.test", "TVE_SMTP_PORT": "465",
                        "TVE_SMTP_USER": "new-user", "TVE_SMTP_PASSWORD": "new-password",
                        "TVE_ENQUIRY_FROM": "new@example.test"})
    await followup.send_journey_email(identity, "booking_link", token="token")
    assert sent[0][0]["host"] == "old-smtp.example.test"
    assert sent[1][0] == {"host": "new-smtp.example.test", "port": 465,
                          "username": "new-user", "password": "new-password", "sender": "new@example.test"}
    assert sent[1][1]["From"] == "Thames Valley Estates <new@example.test>"


class Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.mark.asyncio
async def test_followup_batch_resolves_smtp_for_each_invitation(monkeypatch, credentials):
    class Candidates:
        async def fetch(self, *args):
            return [{"id": id_, "first_name": "Jane", "email": f"jane{id_}@example.test"} for id_ in (1, 2)]

    async def get_pool(name):
        return Pool(Candidates())

    async def issue(*args):
        return "token"

    async def advance(*args):
        pass

    sent = []

    def send(config, message):
        sent.append((config, message))
        credentials["TVE_ENQUIRY_FROM"] = "rotated@example.test"
        credentials["TVE_SMTP_PASSWORD"] = "rotated-password"

    monkeypatch.setattr(followup, "get_pool", get_pool)
    monkeypatch.setattr(followup, "issue_token", issue)
    monkeypatch.setattr(contacts, "advance_status", advance)
    monkeypatch.setattr(followup, "_smtp_send", send)
    assert await followup.deliver_due_followups(None) == 2
    assert sent[0][0]["password"] == "old-password"
    assert sent[1][0]["password"] == "rotated-password"
    assert sent[1][1]["From"] == "Thames Valley Estates <rotated@example.test>"


@pytest.mark.asyncio
async def test_dashboard_resend_reads_dynamic_smtp(monkeypatch, credentials):
    async def contact(id_):
        return {"id": id_, "status": "Questionnaire_sent", "email": "jane@example.test"}

    async def reissue(*args):
        return "token"

    async def lead(id_):
        return {"id": id_}

    sent = []
    monkeypatch.setattr(contacts, "contact", contact)
    monkeypatch.setattr(tokens_store, "reissue_token", reissue)
    monkeypatch.setattr(actions_route, "_refreshed_lead", lead)
    monkeypatch.setattr(followup, "_smtp_send", lambda config, message: sent.append(config))
    await actions_route.resend_invitation(actions_route.ResendInvitationBody(contact_id=7))
    credentials["TVE_SMTP_PASSWORD"] = "rotated-password"
    await actions_route.resend_invitation(actions_route.ResendInvitationBody(contact_id=7))
    assert [config["password"] for config in sent] == ["old-password", "rotated-password"]


@pytest.mark.asyncio
async def test_sandbox_mail_never_reads_live_credentials(monkeypatch, credentials):
    monkeypatch.setenv("sandbox", "TRUE")

    async def forbidden(group):
        pytest.fail("Sandbox mail read real credentials")

    monkeypatch.setattr(settings_store, "get_many", forbidden)
    assert await followup.mail_configuration() == {
        "host": "mock-inbox", "port": 1025, "sender": credentials["TVE_ENQUIRY_FROM"]}
    credentials["TVE_ENQUIRY_FROM"] = "sandbox-rotated@example.test"
    assert (await followup.mail_configuration())["sender"] == "sandbox-rotated@example.test"
    assert await imap_intake._imap_config() is None


@pytest.mark.asyncio
async def test_legacy_links_survive_rewire_until_signing_key_changes(monkeypatch, credentials, token_database):
    monkeypatch.setenv("TVE_TOKEN_KEY", credentials["TVE_TOKEN_KEY"])
    token_database.rows[(7, "questionnaire")] = {
        "id": 1, "contact_id": 7, "token": "legacy-opaque-token",
        "expires_at": datetime.now(timezone.utc) + timedelta(days=7), "consumed_at": None}
    assert (await tokens_store.verify_token("legacy-opaque-token", "questionnaire"))["contact_id"] == 7
    credentials["TVE_TOKEN_KEY"] = "rotated-key"
    assert await tokens_store.verify_token("legacy-opaque-token", "questionnaire") is None


@pytest.mark.asyncio
async def test_guest_issuance_and_verification_read_rotated_key_per_request(monkeypatch, credentials):
    async def upsert(*args):
        return 7

    saved = []

    async def save(**kwargs):
        saved.append(kwargs)
        return {"id": 1}

    monkeypatch.setattr(contacts, "upsert_contact", upsert)
    monkeypatch.setattr(submission_store, "save_submission", save)
    body = guest_route.GuestBody(name="Jane", email="jane@example.test", propertyId=42)
    first = (await guest_route.guest(body))["token"]
    await submissions_route.questionnaire_submit_guest(submissions_route.QuestionnaireBody(token=first))
    credentials["TVE_TOKEN_KEY"] = "new-signing-key"
    with pytest.raises(HTTPException) as error:
        await submissions_route.questionnaire_submit_guest(submissions_route.QuestionnaireBody(token=first))
    assert error.value.status_code == 401
    second = (await guest_route.guest(body))["token"]
    assert verify_questionnaire_token(second, "new-signing-key")["e"] == "jane@example.test"
    await submissions_route.questionnaire_submit_guest(submissions_route.QuestionnaireBody(token=second))
    assert len(saved) == 2


@pytest.fixture
def token_database(monkeypatch):
    class Connection:
        def __init__(self):
            self.rows = {}
            self.lookups = 0

        async def fetchrow(self, sql, *args):
            self.lookups += 1
            if sql.startswith("SELECT token"):
                return self.rows.get((args[0], args[1]))
            if sql.startswith("UPDATE tokens"):
                contact_id, token, expires_at, purpose = args
            elif sql.lstrip().startswith("INSERT"):
                contact_id, purpose, token, expires_at = args
            else:
                assert sql.startswith("SELECT id, contact_id")
                return next((row for (id_, purpose), row in self.rows.items()
                             if row["token"] == args[0] and purpose == args[1]), None)
            row = {"id": 1, "contact_id": contact_id, "token": token,
                   "expires_at": expires_at, "consumed_at": None}
            self.rows[(contact_id, purpose)] = row
            return row

    conn = Connection()

    async def get_pool(name):
        assert name == "tokens"
        return Pool(conn)

    monkeypatch.setattr(tokens_store, "get_pool", get_pool)
    # The token-only fixture has no contacts container. The cross-store fence
    # is exercised separately with a contacts DB fake in test_dashboard_delete.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def existing_contact(contact_id):
        yield

    monkeypatch.setattr(tokens_store, "writable_contact", existing_contact)
    return conn


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["questionnaire", "booking"])
async def test_journey_tokens_invalidate_on_rotation_and_reissue_with_current_key(credentials, token_database, purpose):
    first = await tokens_store.issue_token(7, purpose)
    assert await tokens_store.issue_token(7, purpose) == first
    assert (await tokens_store.verify_token(first, purpose))["contact_id"] == 7
    assert await tokens_store.reissue_token(7, purpose) == first
    credentials["TVE_TOKEN_KEY"] = "new-signing-key"
    lookups = token_database.lookups
    assert await tokens_store.verify_token(first, purpose) is None
    assert token_database.lookups == lookups  # old signatures fail before database access
    second = await tokens_store.issue_token(7, purpose)
    assert second != first
    assert (await tokens_store.verify_token(second, purpose))["contact_id"] == 7
    assert await tokens_store.verify_token(second, "other-purpose") is None
    credentials["TVE_TOKEN_KEY"] = "third-signing-key"
    third = await tokens_store.reissue_token(7, purpose)
    assert third != second
    assert await tokens_store.verify_token(second, purpose) is None
    assert (await tokens_store.verify_token(third, purpose))["contact_id"] == 7


@pytest.mark.asyncio
async def test_followup_issuance_uses_current_key_within_a_batch(monkeypatch, credentials, token_database):
    class Candidates:
        async def fetch(self, *args):
            return [{"id": id_, "first_name": "Jane", "email": f"jane{id_}@example.test"} for id_ in (7, 8)]

    async def get_pool(name):
        return Pool(Candidates())

    async def upsert(name, last_name, email, phone):
        return 7 if email == "jane7@example.test" else 8

    async def advance(*args):
        pass

    sent_tokens = []

    def send(config, message):
        link = next(line for line in message.get_content().splitlines()
                    if line.startswith(followup.QUESTIONNAIRE_URL.rstrip("/") + "/"))
        sent_tokens.append(link.rsplit("/", 1)[1])
        credentials["TVE_TOKEN_KEY"] = "rotated-during-batch"

    monkeypatch.setattr(followup, "get_pool", get_pool)
    monkeypatch.setattr(contacts, "upsert_contact", upsert)
    monkeypatch.setattr(contacts, "advance_status", advance)
    monkeypatch.setattr(followup, "_smtp_send", send)
    assert await followup.deliver_due_followups(None) == 2
    assert tokens_store._valid_signature(sent_tokens[0], "questionnaire", "old-signing-key")
    assert tokens_store._valid_signature(sent_tokens[1], "questionnaire", "rotated-during-batch")
    assert await tokens_store.verify_token(sent_tokens[0], "questionnaire") is None
    assert (await tokens_store.verify_token(sent_tokens[1], "questionnaire"))["contact_id"] == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", ["sbx_" + "a" * 64, "a" * 43])
async def test_legacy_token_is_migrated_when_next_issued(credentials, token_database, legacy):
    token_database.rows[(7, "questionnaire")] = {"token": legacy}
    current = await tokens_store.issue_token(7, "questionnaire")
    assert current != legacy
    assert await tokens_store.verify_token(legacy, "questionnaire") is None
    assert (await tokens_store.verify_token(current, "questionnaire"))["contact_id"] == 7


@pytest.mark.asyncio
async def test_malformed_token_signature_is_rejected_without_database(credentials, token_database):
    for token in ("a" * 43 + "." + "é" * 43, "a.b", "unsigned", ""):
        assert await tokens_store.verify_token(token, "booking") is None
    assert token_database.lookups == 0


@pytest.mark.asyncio
async def test_imap_poll_rotates_credentials_preserves_checkpoints(monkeypatch, credentials, tmp_path):
    message = EmailMessage()
    message["From"] = "Rightmove <autoresponder@rightmove.com>"
    message["Subject"] = "Lettings enquiry: 2 High Street"
    message["Message-ID"] = "<repeat@example.test>"
    message.set_content(
        "Hi, Jane has enquired about this property\n\n2 High Street, Reading, RG1 1AA\n\u00a31,250 pcm\n1 bed\n\n"
        "Jane Applicant\njane@example.test\n07700 900123\nAPPLICANT WOULD LIKE\n    Book a viewing\n")
    raw = message.as_bytes()
    polls, ingested = [], []

    def fetch(config, checkpoint, limit):
        last_uid, baseline = checkpoint.prepare(imap_intake._cursor_identity(config), 777, 1000)
        polls.append((dict(config), last_uid))
        return 777, last_uid, [] if baseline else [(last_uid + 1, raw)]

    async def ingest(pool, raw, **kwargs):
        ingested.append(kwargs)
        return 7

    async def get_pool(name):
        return object()

    async def no_op(*args):
        pass

    changes = [
        {"TVE_IMAP_PASSWORD": "new-password"},
        {"TVE_IMAP_HOST": "new-imap.example.test"},
        {"TVE_IMAP_USERNAME": "NEW-user"},
        {"TVE_IMAP_MAILBOX": "Enquiries"},
    ]

    async def sleep(interval):
        if changes:
            credentials.update(changes.pop(0))
        else:
            raise asyncio.CancelledError

    monkeypatch.delenv("TVE_SETTINGS_TTL_SECONDS", raising=False)
    monkeypatch.setenv("TVE_IMAP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TVE_EMAIL_INTAKE", "imap")  # exercise the legacy poller
    monkeypatch.setattr(imap_intake, "_imap_fetch_batch", fetch)
    monkeypatch.setattr(imap_intake, "ingest_email", ingest)
    monkeypatch.setattr(imap_intake, "deliver_due_followups", no_op)
    monkeypatch.setattr(imap_intake.asyncio, "sleep", sleep)
    monkeypatch.setattr(database, "get_pool", get_pool)
    monkeypatch.setattr(database, "close_pool", no_op)
    with pytest.raises(asyncio.CancelledError):
        await imap_intake.run()
    assert [cursor for config, cursor in polls] == [999, 999, 999, 999, 999]
    assert polls[1][0]["password"] == "new-password"
    assert polls[2][0]["host"] == "new-imap.example.test"
    assert polls[3][0]["username"] == "NEW-user"
    assert polls[4][0]["mailbox"] == "Enquiries"
    assert len(ingested) == 1  # password rotation resumes; new identities baseline
    assert ingested[0]["uid_validity"] == 777
    with imap_intake.CheckpointStore(tmp_path) as checkpoint:
        assert len(checkpoint.state["mailboxes"]) == 4
    assert imap_intake.os.environ["TVE_SETTINGS_TTL_SECONDS"] == "5"


@pytest.mark.asyncio
async def test_imap_failed_ingestion_remains_retryable(monkeypatch, credentials, tmp_path):
    message = EmailMessage()
    message["From"] = "Rightmove <autoresponder@rightmove.com>"
    message["Subject"] = "Lettings enquiry: 2 High Street"
    message["Message-ID"] = "<retry@example.test>"
    message.set_content(
        "Hi, Jane has enquired about this property\n\n2 High Street, Reading, RG1 1AA\n\u00a31,250 pcm\n1 bed\n\n"
        "Jane Applicant\njane@example.test\n07700 900123\nAPPLICANT WOULD LIKE\n    Book a viewing\n")
    monkeypatch.setenv("TVE_IMAP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TVE_EMAIL_INTAKE", "imap")  # exercise the legacy poller
    monkeypatch.setattr(imap_intake, "_imap_fetch_batch", lambda *args: (777, 9, [(10, message.as_bytes())]))

    async def get_pool(name):
        return object()

    attempts = []

    async def ingest(*args, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise RuntimeError("temporary storage failure")
        return 7

    monkeypatch.setattr(database, "get_pool", get_pool)
    monkeypatch.setattr(imap_intake, "ingest_email", ingest)
    config = await imap_intake._imap_config()
    with imap_intake.CheckpointStore(tmp_path) as checkpoint:
        checkpoint.prepare(imap_intake._cursor_identity(config), 777, 10)
    with pytest.raises(RuntimeError):
        await imap_intake.poll_once(None, config)
    with imap_intake.CheckpointStore(tmp_path) as checkpoint:
        assert not checkpoint.state["dedupe"]
        assert checkpoint.state["mailboxes"][imap_intake._cursor_identity(config)]["last_uid"] == 9
    result = await imap_intake.poll_once(None, config)
    assert result == {"fetched": 1, "new_enquiries": 1, "last_uid": 10}
    assert len(attempts) == 2


# --- Long, configurable token TTL (expiry is still enforced) ---

def test_token_ttl_defaults_to_a_year_and_is_configurable():
    assert tokens_store.TOKEN_TTL_DAYS == 365
    assert tokens_store._ttl_days(None) == 365
    assert tokens_store._ttl_days("30") == 30
    assert tokens_store._ttl_days("nonsense") == 365
    assert tokens_store._ttl_days("0") == 365
    assert tokens_store._ttl_days("-5") == 365


@pytest.mark.asyncio
async def test_issued_token_expiry_uses_the_configured_ttl(credentials, token_database):
    before = datetime.now(timezone.utc)
    await tokens_store.issue_token(7, "questionnaire")
    expiry = token_database.rows[(7, "questionnaire")]["expires_at"]
    expected = before + timedelta(days=tokens_store.TOKEN_TTL_DAYS)
    assert abs((expiry - expected).total_seconds()) < 300


@pytest.mark.asyncio
async def test_expired_token_is_still_rejected(credentials, token_database):
    token = await tokens_store.issue_token(7, "questionnaire")
    token_database.rows[(7, "questionnaire")]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert await tokens_store.verify_token(token, "questionnaire") is None

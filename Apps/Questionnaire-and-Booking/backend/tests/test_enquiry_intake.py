from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime

from app.services.enquiries import followup
from app.services.enquiries.matching import match_property
from app.services.enquiries.parser import parse_email
from app.services.shared.contacts import _split_name, clean_name_part
from app.workers import imap_intake


def _mail(*, body: str, subject: str = "Property enquiry", sender: str = "Rightmove <noreply@rightmove.example>",
          reply_to: str = "Jane Applicant <jane@example.test>", message_id: str = "<msg-001@example.test>") -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["Reply-To"] = reply_to
    message["To"] = "lettings@example.test"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = format_datetime(datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc))
    message.set_content(body)
    return message.as_bytes(policy=SMTP)


def test_parses_portal_enquiry_without_retaining_raw_body():
    parsed = parse_email(_mail(body=(
        "Name: Jane Applicant\nEmail: jane@example.test\nPhone: 07700 900123\n"
        "Property: 12 Example Road, Reading, RG1 1AA\nProperty Ref: ABC-123\nI would like to arrange a viewing."
    )))

    assert parsed.is_enquiry is True
    assert parsed.sender_email == "jane@example.test"
    assert parsed.sender_name == "Jane Applicant"
    assert parsed.sender_phone == "07700 900123"
    assert parsed.property_reference == "ABC-123"
    assert parsed.needs_review is False
    assert parsed.confidence >= 0.65
    assert parsed.dedupe_key == "message-id:<msg-001@example.test>"
    assert "raw_body" not in parsed.extracted_fields


def test_html_labels_are_separated_and_portal_sender_is_not_used_as_applicant():
    message = EmailMessage()
    message["From"] = "no-reply@zoopla.example"
    message["Subject"] = "Viewing request"
    message["Message-ID"] = "<html-1@example.test>"
    message.set_content("This message requires HTML")
    message.add_alternative(
        "<html><body><p>Name: Sam Lead</p><p>Email: sam@example.test</p>"
        "<p>Property: 8 Market Street, Reading</p></body></html>", subtype="html"
    )

    parsed = parse_email(message.as_bytes(policy=SMTP))
    assert parsed.sender_name == "Sam Lead"
    assert parsed.sender_email == "sam@example.test"
    assert parsed.property_query == "8 Market Street, Reading"
    assert parsed.is_enquiry is True
    assert parsed.extracted_fields["platform"] == "zoopla"


def test_onthemarket_semicolon_footer_names_are_cleaned():
    parsed = parse_email(_mail(
        sender="OnTheMarket <no-reply@onthemarket.example>",
        body=(
            "You have a new enquiry\n"
            "Name:Fraser Gibson;\nAddress:;\nEmail:jane@example.test;\nPhone:07700 900123;\n"
        ),
    ))

    assert parsed.sender_name == "Fraser Gibson"

    unlabelled = parse_email(_mail(
        sender="OnTheMarket <no-reply@onthemarket.example>",
        message_id="<msg-002@example.test>",
        body="You have a new enquiry\nFraser Gibson;\n\njane@example.test;\n",
    ))

    assert unlabelled.sender_name == "Fraser Gibson"
    assert _split_name("Fraser Gibson;", "") == ("Fraser", "Gibson")
    assert _split_name("", "Gibson;") == ("", "Gibson")
    assert clean_name_part("Gibson;") == "Gibson"
    assert clean_name_part("d'Orton-Gibson; ") == "d'Orton-Gibson"


def test_duplicate_message_id_produces_same_dedupe_key_and_non_enquiry_is_ignored():
    first = parse_email(_mail(body="Name: Lee\nEmail: lee@example.test\nProperty: 2 High Street"))
    second = parse_email(_mail(body="A different MIME body with the same Message-ID"))
    noise = parse_email(_mail(subject="Monthly newsletter", body="Read our latest news", message_id="<noise@example.test>"))

    assert first.dedupe_key == second.dedupe_key
    assert noise.is_enquiry is False


def test_non_enquiry_thread_quoting_a_property_is_not_an_enquiry():
    """Regression: a legal thread quoting a real property address must not be
    classified as an enquiry nor expose a matchable property.

    Mirrors the live Spencer West email that was force-matched to a property
    and sent a questionnaire.
    """
    body = (
        "Morning Maqsood,\n\n"
        "The email he will need to address is directly from Docusign containing our client terms and conditions.\n\n"
        "Sadia Razaque\nJunior Associate\n"
        "Real Estate: if you are purchasing a property, fraudsters may target you.\n"
        "Longbow House, 20 Chiswell Street, London EC1Y 4TW\n\n"
        "From: Maqsood\nSubject: RE: Memorandum of Agreed Sale for 52 Howth Drive, Reading, RG5 3EG\n\n"
        "The price of the property has been renegotiated from \u00a3450,000 to \u00a3445,000.\n"
        "24 Cholmeley Road, Reading, Berkshire, RG1 3NQ\n"
        "52 Howth Drive, Reading, RG5 3EG\n"
    )
    parsed = parse_email(_mail(
        sender="Sadia Razaque <sadia.razaque@spencer-west.com>",
        reply_to="Sadia Razaque <sadia.razaque@spencer-west.com>",
        subject="RE: Memorandum of Agreed Sale for 52 Howth Drive, Reading, RG5 3EG",
        body=body,
        message_id="<spencer-west@example.test>",
    ))

    assert parsed.is_enquiry is False
    # No matchable property address may be scraped from a quoted thread.
    assert parsed.property_query is None
    assert parsed.property_reference is None


def test_marketing_and_footer_mail_is_not_an_enquiry():
    for body, sender in (
        ("A refreshingly different approach to selling. We are property experts. Contact us.", "reply@kings-brook.co.uk"),
        ("I am the Relationship Director at LMS and will be in your area meeting forward-thinking agents.", "benjamin.smith@lms.com"),
        ("Please change your password as a precaution. If you received this email but it was not you...", "fraud@rightmove.co.uk"),
    ):
        parsed = parse_email(_mail(body=body, sender=sender, reply_to=sender, message_id=f"<{sender}@example.test>"))
        assert parsed.is_enquiry is False, body


def test_explicit_applicant_intent_is_an_enquiry_even_without_a_labelled_address():
    parsed = parse_email(_mail(
        sender="applicant@example.test",
        reply_to="applicant@example.test",
        subject="Viewing request",
        body="Hello, I would like to arrange a viewing for the flat in Reading please.",
        message_id="<intent@example.test>",
    ))
    assert parsed.is_enquiry is True


def test_unknown_sender_with_a_property_address_is_not_auto_linked():
    """A non-portal sender whose mail merely contains a street address must not
    be turned into a matchable property query."""
    parsed = parse_email(_mail(
        sender="someone@random-law-firm.example",
        reply_to="someone@random-law-firm.example",
        subject="RE: matters",
        body="Please reply regarding 52 Howth Drive, Reading, RG5 3EG and the contract.",
        message_id="<unknown@example.test>",
    ))
    assert parsed.is_enquiry is False
    assert parsed.property_query is None


# --- Lettings-only guard: sales/tenant/account mail must never be enquiries ---

def test_portal_enquiry_template_outranks_footer_boilerplate():
    """Regression: a genuine portal enquiry whose legal footer contains words
    like 'landlord' (e.g. "Rightmove Landlord and Tenant Services Limited")
    must still be an enquiry. Mirrors the live Mason Street enquiry that was
    missed because 'landlord' suppressed it."""
    parsed = parse_email(_mail(
        sender="Rightmove User Manager <autoresponder@rightmove.com>",
        reply_to="Rightmove User Manager <autoresponder@rightmove.com>",
        subject="Lettings enquiry: Mason Street - Tenant from CM1",
        body=(
            "Hi, scott has enquired about this property\n\n"
            "32, Mason Street, Reading, RG1 7PD\n\u00a3850 pcm\n\n"
            "scott lemon\nscott_lemon@hotmail.co.uk\n07983692863\n"
            "26, Newcombe Court, Burgess Springs, Chelmsford, Essex, CM1 1QN, CM1 1QN\n"
            "MOVE IN DATE\nFlexible\nPETS\nYes\nAPPLICANT WOULD LIKE\n    Book a viewing\n\n"
            "Rightmove Group Limited (RMG), Firm Reference No. 491645, is an Appointed "
            "Representative of Rightmove Landlord and Tenant Services Limited (RLTS).\n"
        ),
        message_id="<mason-street@example.test>",
    ))
    assert parsed.is_enquiry is True
    assert "Mason Street" in (parsed.property_query or "")


def test_rightmove_valuation_request_is_not_a_lettings_enquiry():
    """Regression: an OnTheMarket valuation/sales request is not a lettings
    enquiry even though it references an address and 'valuation request'.
    Mirrors the live barlows311 lead."""
    parsed = parse_email(_mail(
        sender="OnTheMarket.com <no-reply@expert.onthemarket.com>",
        reply_to="OnTheMarket.com <no-reply@expert.onthemarket.com>",
        subject="Valuation enquiry from Sarah Barlow",
        body=(
            "You have received a valuation request from Sarah Barlow.\n\n"
            "Contact information\nSarah Barlow\n07708 921689\nbarlows311@gmail.com\n47 Callington Road, RG2 7QH\n\n"
            "Enquiry details\n\n- I would like: a valuation of a property I would like to sell\n- Enquiry type: sales\n\n"
            "PropertyToBeValued\nProperty address: 47 Callington Road, Reading, Berkshire, RG2 7QH\n"
            "PropAddress:47 Callington Road, Reading, Berkshire, RG2 7QH;\n"
        ),
        message_id="<valuation@example.test>",
    ))
    assert parsed.is_enquiry is False


def test_tenant_account_and_tenancy_mail_is_not_a_lettings_enquiry():
    """Regression: tenant replies referencing their property must not become
    lettings enquiries. Mirrors live terterov (account statement) and
    vignesh.jadhav (tenancy agreement) leads."""
    for body, subject, sender in (
        ("Dear Zubair - Many thanks for this statement. Any news on the balance of the rental payment "
         "for September for 3 JBC? Property Account Statement.", "Re: Property Account Statement", "terterov@googlemail.com"),
        ("Hi Maqsood, Could you please send me a copy of the fully signed tenancy agreement for "
         "Top Floor Studio, 13 Charles Street, Reading, RG1 7DB?", "Re: Tenancy Agreement (Signed)", "vignesh.jadhav@gmail.com"),
        ("Draft Tenancy and SLA for 84 Blenhim Road - please review the property agreement.",
         "Draft Tenancy and SLA for 84 Blenhim Road", "judithdyer@totalhealthandtraining.co.uk"),
    ):
        parsed = parse_email(_mail(body=body, subject=subject, sender=sender, reply_to=sender,
                                   message_id=f"<{sender}@example.test>"))
        assert parsed.is_enquiry is False, subject


# --- Property extraction: portal property line that does not start with a digit ---

def test_rightmove_flat_property_line_is_extracted_not_applicant_address():
    """Regression: the Yan Yang enquiry. The portal property line starts with
    'Flat 1' (no leading digit) and appears before the applicant's own address.
    Extraction must take the portal property line, not the applicant address."""
    parsed = parse_email(_mail(
        sender="Rightmove User Manager <autoresponder@rightmove.com>",
        reply_to="Rightmove User Manager <autoresponder@rightmove.com>",
        subject="Lettings enquiry: Wokingham Road - Tenant from RG6",
        body=(
            "Hi, Yan has enquired about this property\n\n"
            "Flat 1, Wokingham Road, Reading, RG6 1JG\n"
            "\u00a31,150 pcm\n1 bed\n\n"
            "Yan Yang\nlaurayangyan@126.com\n07546878859\n"
            "9, Lydford Road, Reading, RG1 5QH, RG1 5QH\n"
            "MOVE IN DATE\nWithin 2 weeks\n"
            "APPLICANT WOULD LIKE\n    More details on this property\n    Book a viewing\n"
        ),
        message_id="<yan-yang@example.test>",
    ))
    assert parsed.is_enquiry is True
    assert parsed.property_query is not None
    assert "Wokingham" in parsed.property_query
    assert "Lydford" not in parsed.property_query


def test_message_without_id_uses_content_hash():
    message = EmailMessage()
    message["From"] = "test@example.invalid"
    message.set_content("Property enquiry")
    parsed = parse_email(message.as_bytes())
    assert parsed.dedupe_key.startswith("sha256:")


def test_property_match_requires_unique_exact_reference_or_address():
    properties = [
        {"id": 12, "altReference": "ABC-123", "street": "12 Example Road", "town": "Reading", "postcode": "RG1 1AA"},
        {"id": 13, "altReference": "DEF-456", "street": "12 Example Road", "town": "Reading", "postcode": "RG1 1AA"},
    ]

    assert match_property(None, "ABC-123", properties) == {"status": "matched", "property_id": 12, "candidates": [12]}
    assert match_property("12 Example Road, Reading, RG1 1AA", None, properties)["status"] == "ambiguous"
    assert match_property("the town centre", None, properties)["status"] == "unmatched"


def test_matching_priority_reference_then_address_then_postcode_street():
    # Reference wins when unique; a shared reference is disambiguated by a
    # unique exact address; still-ambiguous stays for manual selection.
    props = [
        {"id": 10, "altReference": "REF-10", "street": "1 Alpha Road", "town": "Reading", "postcode": "RG1 1AA"},
        {"id": 11, "altReference": "REF-11", "street": "2 Beta Road", "town": "Reading", "postcode": "RG2 2BB"},
        {"id": 12, "altReference": "REF-11", "street": "3 Gamma Road", "town": "Reading", "postcode": "RG3 3CC"},
    ]
    assert match_property(None, "REF-10", props) == {"status": "matched", "property_id": 10, "candidates": [10]}
    # Shared reference + unique exact address resolves to one property.
    assert match_property("2 Beta Road, Reading, RG2 2BB", "REF-11", props) == {
        "status": "matched", "property_id": 11, "candidates": [11]}
    # Shared reference with no disambiguating address -> manual selection.
    assert match_property(None, "REF-11", props)["status"] == "ambiguous"
    # Exact address when no reference.
    assert match_property("2 Beta Road, Reading, RG2 2BB", None, props)["status"] == "matched"
    # Unique postcode+street (house number / flat qualifier ignored).
    assert match_property("Flat 1, 99 Alpha Road, Reading, RG1 1AA", None, props) == {
        "status": "matched", "property_id": 10, "candidates": [10]}



def test_production_mail_uses_credentials_without_enable_flags(monkeypatch):
    monkeypatch.delenv("SANDBOX", raising=False)
    monkeypatch.setenv("sandbox", "FALSE")
    for key, value in {"TVE_SMTP_HOST": "smtp.example.test", "TVE_SMTP_PORT": "587",
                       "TVE_SMTP_USER": "mail-user", "TVE_SMTP_PASSWORD": "test-password",
                       "TVE_ENQUIRY_FROM": "agent@example.test"}.items():
        monkeypatch.setenv(key, value)
    assert followup._mail_configuration() == {
        "host": "smtp.example.test", "port": 587, "username": "mail-user",
        "password": "test-password", "sender": "agent@example.test"}
    for key, value in {"TVE_IMAP_HOST": "imap.example.test", "TVE_IMAP_USERNAME": "mail-user",
                       "TVE_IMAP_PASSWORD": "test-password"}.items():
        monkeypatch.setenv(key, value)
    assert imap_intake.inbound_configuration()["host"] == "imap.example.test"


def test_production_smtp_authenticates_and_sends(monkeypatch):
    monkeypatch.delenv("SANDBOX", raising=False)
    monkeypatch.setenv("sandbox", "FALSE")
    calls = []
    class SMTP:
        def __init__(self, host, port, **kwargs): calls.append((host, port))
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ehlo(self): calls.append("ehlo")
        def starttls(self, **kwargs): calls.append("starttls")
        def login(self, username, password): calls.append((username, password))
        def send_message(self, message): calls.append("sent")
    monkeypatch.setattr(followup.smtplib, "SMTP", SMTP)
    followup._smtp_send({"host": "smtp.example.test", "port": 587,
                         "username": "mail-user", "password": "test-password"}, object())
    assert calls == [("smtp.example.test", 587), "ehlo", "starttls", "ehlo",
                     ("mail-user", "test-password"), "sent"]


def test_invitation_has_professional_copy_and_tokenized_link():
    from app.services.enquiries.followup import _invitation_message
    message = _invitation_message(55, "Jane Applicant", "jane@example.test", "signed.test-token",
                                  "http://127.0.0.1:3001/questionnaire", "TVE Sandbox <sandbox@example.test>")

    assert message["Message-ID"] == "<tve-questionnaire-55@thamesvalleyestates.co.uk>"
    assert message["From"] == "Thames Valley Estates <sandbox@example.test>"
    assert message["To"] == "jane@example.test"
    assert message.get_content_type() == "text/plain"
    assert "Dear Jane Applicant" in message.get_body(preferencelist=("plain",)).get_content()
    from app.services.shared import tokens_store
    assert f"valid for {tokens_store.TOKEN_TTL_DAYS} days" in message.get_body(preferencelist=("plain",)).get_content()
    assert "please complete our short questionnaire" in message.get_body(preferencelist=("plain",)).get_content()
    assert "http://127.0.0.1:3001/questionnaire/signed.test-token" in message.get_body(preferencelist=("plain",)).get_content()


def test_imap_fetch_uses_tls_readonly_and_never_mutates_mailbox(monkeypatch, tmp_path):
    class FakeImap:
        def __init__(self, *args, **kwargs):
            self.calls = []

        def login(self, username, password):
            self.calls.append(("login", username))
            return "OK", []

        def select(self, mailbox, readonly=False):
            self.calls.append(("select", mailbox, readonly))
            return "OK", [b"2"]

        def response(self, name):
            return name, [b"777" if name == "UIDVALIDITY" else b"104"]

        def uid(self, command, *args):
            self.calls.append((command, *args))
            if command == "search":
                return "OK", [b"101 102 103"]
            return "OK", [(b"RFC822", _mail(body="Name: Jane").replace(b"\r\n", b"\n"))]

        def logout(self):
            self.calls.append(("logout",))

    fake = FakeImap()
    monkeypatch.setattr(imap_intake.imaplib, "IMAP4_SSL", lambda *args, **kwargs: fake)
    config = {"host": "imap.example.test", "port": 993, "username": "mailbox", "password": "hidden", "mailbox": "INBOX"}
    with imap_intake.CheckpointStore(tmp_path) as checkpoint:
        checkpoint.prepare(imap_intake._cursor_identity(config), 777, 101)
        validity, cursor, messages = imap_intake._imap_fetch_batch(config, checkpoint, limit=5)

    assert (validity, cursor) == (777, 100)
    assert [uid for uid, _ in messages] == [101, 102, 103]
    assert ("select", "INBOX", True) in fake.calls
    assert [(call[1], call[2]) for call in fake.calls if call[0] == "fetch"] == [
        (str(uid), "(BODY.PEEK[])") for uid in (101, 102, 103)]
    assert not any(call[0] in {"store", "copy", "move", "delete", "expunge"} for call in fake.calls)

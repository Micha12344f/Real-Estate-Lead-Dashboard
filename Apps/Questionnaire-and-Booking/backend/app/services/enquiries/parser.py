"""Privacy-conscious MIME parsing and deterministic property-enquiry extraction."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from ..shared.contacts import clean_name_part

PARSER_VERSION = "tve-enquiry-rules-v5"
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ()\-.]{7,}\d)(?!\w)")
_PROPERTY_REF = re.compile(
    r"(?i)\b(?:propReference|your agency ref|property\s*(?:ref(?:erence)?|id)|listing\s*ref(?:erence)?|reference)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_-]{2,})"
)
_FIELD_PATTERNS = {
    "name": re.compile(r"(?im)^\s*(?:name|applicant name|your name)\s*[:\-]\s*(.{2,100})\s*$"),
    "email": re.compile(r"(?im)^\s*(?:email|e-mail|email address)\s*[:\-]\s*([^\s<>]+@[^\s<>]+)\s*$"),
    "phone": re.compile(r"(?im)^\s*(?:phone|telephone|mobile|tel)\s*[:\-]\s*([+\d][\d ()\-.]{6,25})\s*$"),
    "property": re.compile(r"(?im)^\s*(?:PropAddress|property|property address|interested in|listing)\s*[:\-]\s*(.{3,180})\s*$"),
}
# Known portal senders. Only mail that actually originated from one of these
# may use its unlabelled template layout; the subject line alone is not proof.
_PORTAL_SENDER_MARKERS = ("rightmove", "zoopla", "onthemarket", "on-the-market", "expert.onthemarket")

# Labelled footer keys. Their presence confirms a real portal template was
# used, but only alongside a genuine portal sender and a property field.
_LABELLED_ENQUIRY_FIELDS = (
    "propaddress", "propreference", "propdescription", "searchcriteria",
    "propstatus", "propprice",
)

# Template phrases emitted only by the portals' own enquiry-notification
# emails. A portal message counts as a template only with one of these.
_PORTAL_TEMPLATE_FIELDS = (
    "lettings enquiry", "letting enquiry",
    "enquirytype:lettings", "enquiry type: lettings", "enquirytype: lettings",
    "details request", "viewing enquiry", "viewing request",
    "has enquired about this property", "enquired about this property",
)

# Direct (non-portal) emails are accepted only on explicit rental or viewing
# intent. Merely mentioning a property or an address is never enough.
_DIRECT_INTENT_MARKERS = (
    "would like to rent", "want to rent", "looking to rent", "looking for a rental",
    "interested in renting", "wish to rent", "hope to rent",
    "would like to arrange a viewing", "arrange a viewing", "book a viewing",
    "request a viewing", "schedule a viewing", "interested in viewing",
    "would like to view", "like to view the property", "arrange to view",
    "arrange a viewing of", "viewing on",
)

# Known non-enquiry categories. Any message carrying one of these is rejected
# outright, even when it references or matches a property address. Multi-word
# and long tokens only; short ambiguous tokens are matched with word
# boundaries below.
_NON_LETTINGS_MARKERS = (
    "valuation request", "valuation enquiry", "enquiry type: sales", "enquirytype:sales",
    "enquirytype: sales", "i would like to sell", "want to sell", "property valuation",
    "free valuation", "sales valuation", "market appraisal", "valuation of a property",
    "tenancy agreement", "tenancy renewal", "tenancy application", "tenant application",
    "property account statement", "account statement", "rent statement",
    "rent payment", "standing order", "rent arrears", "balance of the rental payment",
    "maintenance", "repairs", "damp", "leak", "boiler", "heating",
    "docusign", "signable", "signed the", "sign the agreement",
    "landlord", "vendor", "solicitor", "conveyanc", "draft tenancy",
)

# Short tokens that only count as exclusions when they appear as whole words.
_NON_LETTINGS_WORDS = (
    "invoice", "repair", "sla", "arrears", "damp", "leak", "boiler",
)


class _TextOnlyHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "head"}:
            self._hidden += 1
        elif tag in {"br", "p", "div", "tr", "td", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "head"} and self._hidden:
            self._hidden -= 1
        elif tag in {"p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


def _part_text(part) -> str:
    try:
        value = part.get_content()
    except (LookupError, UnicodeError, AttributeError):
        raw = part.get_payload(decode=True) or b""
        value = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
    if not isinstance(value, str):
        return ""
    if part.get_content_type() == "text/html":
        parser = _TextOnlyHTML()
        try:
            parser.feed(value)
            value = " ".join(parser.parts)
        except Exception:
            value = re.sub(r"<[^>]{0,500}>", " ", value)
    return html.unescape(value)


def _body_text(message) -> str:
    plain: list[str] = []
    rich: list[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_maintype() != "text" or part.get_content_disposition() == "attachment":
            continue
        text = _part_text(part)
        if part.get_content_type() == "text/plain":
            plain.append(text)
        elif part.get_content_type() == "text/html":
            rich.append(text)
    # Some portal templates ship a boilerplate plain alternative alongside a
    # useful HTML body. Include both so field extraction can use either form.
    return "\n".join([*plain, *rich])


def _header_text(value: str | None) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()[:500]


def _first(pattern: re.Pattern, text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1).strip(" \t,;.") if match else None


@dataclass(frozen=True)
class ParsedEnquiry:
    message_id: str
    received_at: datetime
    subject: str
    sender_name: str | None
    sender_email: str | None
    sender_phone: str | None
    enquiry_type: str | None
    property_query: str | None
    property_reference: str | None
    is_enquiry: bool
    confidence: float
    needs_review: bool
    extracted_fields: dict
    dedupe_key: str
    parser_version: str = PARSER_VERSION
    body: str | None = None  # parsed body text; set by parse_email, never logged

    def as_dict(self) -> dict:
        result = asdict(self)
        result["received_at"] = self.received_at
        return result


def parse_email(raw_message: bytes) -> ParsedEnquiry:
    """Parse one RFC822 message without retaining or logging its raw body."""
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    subject = _header_text(message.get("Subject"))
    body = _body_text(message)
    parsed = _parse_parts(message, subject, body, raw_message)
    object.__setattr__(parsed, "body", body)
    return parsed


def _parse_parts(message, subject: str, body: str, raw_message: bytes) -> ParsedEnquiry:
    reply_name, reply_email = parseaddr(_header_text(message.get("Reply-To")))
    from_name, from_email = parseaddr(_header_text(message.get("From")))

    def first_field(name: str) -> str | None:
        return _first(_FIELD_PATTERNS[name], body)

    # OTM semi-colon fields and Rightmove's unlabeled contact block are
    # preserved by the colleague's templates. Parse those original formats.
    contact_email_match = _EMAIL.search(body)

    body_email = first_field("email")
    if not body_email:
        # Only accept an unlabeled email from the body when the source template
        # clearly marks it as the applicant's contact address.
        contact_line = re.search(r"(?im)^\s*(?:applicant|contact|customer)\s+(?:email|e-mail)\s*[:\-]\s*([^\s<>]+@[^\s<>]+)", body)
        body_email = contact_line.group(1) if contact_line else None
    candidate_from = reply_email or from_email
    if candidate_from and re.search(r"(?i)(?:no[._-]?reply|do[._-]?not[._-]?reply|rightmove|zoopla|onthemarket|on-the-market)", candidate_from):
        candidate_from = ""
    # Only a genuine portal sender may use the portal's unlabelled layout.
    # A portal name in the subject does not make the sender a portal.
    sender_is_portal = any(marker in (from_email or "").lower() for marker in _PORTAL_SENDER_MARKERS)
    sender_email = (body_email or (contact_email_match.group(0) if sender_is_portal and contact_email_match else None) or candidate_from or None)
    sender_email = sender_email.strip().lower() if sender_email else None
    sender_name = first_field("name") or reply_name or from_name or None
    if sender_is_portal and not first_field("name") and contact_email_match:
        preceding = body[:contact_email_match.start()].splitlines()
        sender_name = next((line.strip() for line in reversed(preceding) if line.strip()), sender_name)
    sender_name = clean_name_part(sender_name)[:160] or None
    phone = first_field("phone")
    if not phone:
        phone_match = _PHONE.search(body)
        phone = phone_match.group(0).strip() if phone_match else None
    if phone:
        phone = re.sub(r"\s+", " ", phone)[:40]

    property_query = first_field("property")
    # The unlabelled layouts below are portal-template shapes. Applying them to
    # arbitrary mail scrapes quoted addresses out of legal/marketing threads and
    # forces a bogus match, so they are gated on a genuine portal sender.
    if not property_query and sender_is_portal:
        # The portal property line is the FIRST address line in the body. It may
        # not start with a digit ("Flat 1, Wokingham Road, ..."), so match any
        # line ending in a UK postcode and take the first one. This avoids
        # grabbing the applicant's own address, which appears later.
        for line in body.splitlines():
            candidate = re.match(
                r"(?i)^\s*(.+?,\s*[^,\n]+,\s*[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\s*(?:-[^\n]*)?$",
                line.strip())
            if candidate:
                property_query = candidate.group(1).strip()
                break
    if not property_query and sender_is_portal:
        property_query = next((line.strip() for line in (subject, *body.splitlines())
                               if re.search(r"(?i)\b(property|flat|house|apartment|road|street|lane|avenue|close|court)\b", line)
                               and len(line.strip()) <= 180), None)
    property_query = re.sub(r"\s+", " ", property_query).strip()[:180] if property_query else None
    reference_match = _PROPERTY_REF.search("\n".join((subject, body[:12000])))
    property_reference = reference_match.group(1).strip(" .,:;-").upper()[:48] if reference_match else None

    lower = f"{subject}\n{body[:12000]}".lower()

    # --- Strict template detection -----------------------------------------
    # A message is a lettings enquiry when it is a real enquiry template from a
    # known portal sender (template phrase plus an expected field), or a direct
    # email carrying explicit rental or viewing intent. Known non-enquiry
    # categories are rejected, but a genuine portal template outranks the
    # exclusion list: portal legal footers mention words such as "landlord" or
    # "vendor" (e.g. "Rightmove Landlord and Tenant Services Limited") and must
    # not suppress a real enquiry from that portal.
    labelled_property = bool(first_field("property"))
    labelled_footer = any(field in lower for field in _LABELLED_ENQUIRY_FIELDS)
    direct_intent = any(marker in lower for marker in _DIRECT_INTENT_MARKERS)

    portal_phrase = any(phrase in lower for phrase in _PORTAL_TEMPLATE_FIELDS)
    has_expected_field = bool(
        labelled_property or labelled_footer or body_email
        or property_query or property_reference)
    strong_portal = bool(sender_is_portal and portal_phrase and has_expected_field)

    non_lettings = any(marker in lower for marker in _NON_LETTINGS_MARKERS)
    if not non_lettings:
        non_lettings = any(re.search(rf"\b{re.escape(word)}\b", lower) for word in _NON_LETTINGS_WORDS)

    if strong_portal:
        # A genuine portal enquiry template with its expected field; accept even
        # if footer boilerplate contains otherwise-excluded words.
        is_enquiry = True
    elif non_lettings:
        # Explicitly rejected category (sales/valuation, tenant, account, legal).
        is_enquiry = False
    else:
        # Direct email: accept only on explicit rental or viewing intent. A
        # property/address mention on its own is never enough.
        is_enquiry = bool(direct_intent)

    evidence = sum((bool(sender_email), bool(sender_name), bool(phone), bool(property_query), bool(property_reference)))
    confidence = min(1.0, (0.45 if is_enquiry else 0.0) + evidence * 0.11)
    # Portal-generated sender addresses are not reliable applicant addresses;
    # explicit body/Reply-To values take precedence and unknowns go to review.
    needs_review = not (sender_email and property_query) or confidence < 0.65
    raw_id = _header_text(message.get("Message-ID"))
    dedupe_key = f"message-id:{raw_id.strip().lower()}" if raw_id else f"sha256:{hashlib.sha256(raw_message).hexdigest()}"
    date_header = message.get("Date")
    try:
        received_at = parsedate_to_datetime(str(date_header)).astimezone(timezone.utc) if date_header else datetime.now(timezone.utc)
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        received_at = datetime.now(timezone.utc)

    message_id = raw_id[:998] if raw_id else ""
    extracted = {
        "property_reference": property_reference,
        "has_sender_name": bool(sender_name),
        "has_sender_email": bool(sender_email),
        "has_sender_phone": bool(phone),
        "platform": "rightmove" if "rightmove" in from_email.lower() else "zoopla" if "zoopla" in from_email.lower() else "onthemarket" if "onthemarket" in from_email.lower() else "email",
    }
    for heading in ("MOVE IN DATE", "DURATION", "MOVING WITH", "PETS", "SMOKERS", "EMPLOYMENT", "AFFORDABILITY", "ADVERSE CREDIT", "APPLICANT WOULD LIKE"):
        value = re.search(r"(?im)^" + re.escape(heading) + r"\s*\n([^\n]+)", body)
        if value:
            extracted[heading.lower().replace(" ", "_")] = value.group(1).strip()[:200]
    return ParsedEnquiry(
        message_id=message_id, received_at=received_at, subject=subject, sender_name=sender_name,
        sender_email=sender_email, sender_phone=phone, enquiry_type="property" if is_enquiry else None,
        property_query=property_query, property_reference=property_reference, is_enquiry=is_enquiry,
        confidence=round(confidence, 4), needs_review=needs_review, extracted_fields=extracted,
        dedupe_key=dedupe_key,
    )

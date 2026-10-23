"""Inbound email intake: parse, match and persist one raw message.

Neutral home for the shared intake pipeline used by the webhook route, the
sandbox Mailpit adapter and the legacy IMAP worker. Credentials and mailbox
transport live elsewhere; this module only turns a raw RFC822 message into a
persisted enquiry (and its property match).
"""

from __future__ import annotations

from .parser import parse_email
from .matching import match_property
from .repository import record_message


async def ingest_email(pool, raw: bytes, *, uid: int, mailbox_key: str, mailbox_name: str,
                       uid_validity: int) -> int | None:
    """Parse, match and persist one raw message; return the contact id or None.

    Matching runs only for genuine enquiries. A failed property lookup is
    recorded as ``lookup_error`` so the enquiry is kept but not mis-linked.
    """
    parsed = parse_email(raw)
    match = None
    lookup_error = False
    if parsed.is_enquiry:
        try:
            from ..shared.acquaint import client
            from ..booking.properties import is_selectable_property
            properties = [p for p in await client.list_properties() if is_selectable_property(p)]
            match = match_property(parsed.property_query, parsed.property_reference, properties)
        except Exception:
            lookup_error = True
    return await record_message(pool, mailbox_key=mailbox_key, mailbox_name=mailbox_name,
                                uid_validity=uid_validity, uid=uid, parsed=parsed,
                                property_match=match, lookup_error=lookup_error)

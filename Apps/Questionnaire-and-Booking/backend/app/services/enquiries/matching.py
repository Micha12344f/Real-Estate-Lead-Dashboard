"""Conservative matching of extracted enquiry references/addresses to Acquaint properties."""

from __future__ import annotations

import re
from collections.abc import Mapping

from ..booking.properties import normalise_property

_POSTCODE = re.compile(r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}", re.IGNORECASE)
# Tokens that carry no locational value when comparing a portal line to a
# catalogue address: flat/unit qualifiers.
_NON_LOCATIONAL = {"flat", "unit", "room", "apartment", "apt", "studio", "the"}


def _norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _postcode(value: object) -> str:
    match = _POSTCODE.search(str(value or ""))
    return re.sub(r"\s+", "", match.group(0).upper()) if match else ""


def _street_key(value: object) -> str:
    """Normalised address with the house number and flat qualifier removed.

    Portal lines name the unit ("Flat 1, Wokingham Road") while the CRM record
    names the building ("14 Wokingham Road"); dropping numeric and flat tokens
    lets the same street be compared. The postcode is still required to match
    separately, so different streets never collide.
    """
    tokens = [tok for tok in _norm(value).split() if tok and tok not in _NON_LOCATIONAL and not tok.isdigit()]
    return " ".join(tokens)


def _property_reference(raw: Mapping) -> str:
    return _norm(raw.get("altReference") or raw.get("propertyReference") or raw.get("reference"))


def _address(raw: Mapping) -> str:
    return _norm(normalise_property(dict(raw)).get("address"))


def _raw_address(raw: Mapping) -> str:
    return str(normalise_property(dict(raw)).get("address") or "")


def match_property(query: str | None, reference: str | None, properties: list[dict]) -> dict:
    """Match a property in a fixed priority order.

    1. Acquaint reference (unique exact match on ``altReference``).
    2. Exact address (normalised full address equal).
    3. Unique postcode-and-street match (same postcode and same street tokens
       after dropping house numbers and flat/unit qualifiers).

    The stages are tried in order and the first that yields exactly one property
    wins. A stage that yields several candidates does not force a guess: the next
    stage is used to try to disambiguate (e.g. a shared reference resolved by a
    unique exact address). If no stage resolves to a single property but at least
    one produced candidates, the result is **ambiguous** and requires manual
    selection. Nothing at all -> unmatched.
    """
    query_norm = _norm(query)
    ref_norm = _norm(reference)
    if not query_norm and not ref_norm:
        return {"status": "unmatched", "property_id": None, "candidates": []}

    query_pc = _postcode(query)
    query_key = _street_key(query)

    references: list[dict] = []
    addresses: list[dict] = []
    streets: list[dict] = []
    for raw in properties:
        if not isinstance(raw, Mapping) or raw.get("id") is None:
            continue
        if ref_norm and _property_reference(raw) == ref_norm:
            references.append(raw)
        if query_norm:
            address = _address(raw)
            if address and query_norm == address:
                addresses.append(raw)
        if query_pc and query_key:
            raw_address = _raw_address(raw)
            if _postcode(raw_address) == query_pc and _street_key(raw_address) == query_key:
                streets.append(raw)

    def ids_of(items: list[dict]) -> list[int]:
        return sorted({int(item["id"]) for item in items if str(item["id"]).isdigit()})

    stage_candidates = [ids_of(references), ids_of(addresses), ids_of(streets)]
    for ids in stage_candidates:
        if len(ids) == 1:
            return {"status": "matched", "property_id": ids[0], "candidates": ids}
    # No stage resolved to a single property. If any stage found candidates,
    # the highest-priority non-empty set is ambiguous and needs manual review.
    for ids in stage_candidates:
        if ids:
            return {"status": "ambiguous", "property_id": None, "candidates": ids[:10]}
    return {"status": "unmatched", "property_id": None, "candidates": []}

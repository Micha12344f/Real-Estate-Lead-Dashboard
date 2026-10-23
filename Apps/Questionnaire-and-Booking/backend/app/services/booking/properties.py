"""Booking property data normalization."""


def is_selectable_property(raw: dict) -> bool:
    """Allow only properties whose status ID is 0 or 1 (including numeric strings)."""
    status_id = raw.get("statusId")
    if status_id is None or isinstance(status_id, bool):
        return False
    try:
        return int(status_id) in (0, 1) and float(status_id) == int(status_id)
    except (TypeError, ValueError, OverflowError):
        return False


def normalise_property(raw: dict) -> dict:
    """Mirror of n8n 'Code - Normalise Properties'."""
    raw_address = raw.get("address") if isinstance(raw.get("address"), dict) else {}
    street = raw.get("street") or raw_address.get("street")
    town = raw.get("town") or raw_address.get("town")
    postcode = raw.get("postcode") or raw_address.get("postcode")
    property_name = raw.get("propertyName") or raw_address.get("propertyName")
    address = ", ".join(str(part).strip() for part in (street, town, postcode) if part)
    return {
        "id": raw.get("id"),
        "statusId": raw.get("statusId"),
        "propertyTypeId": raw.get("propertyTypeId"),
        "price": raw.get("price"),
        "pricePrefix": raw.get("pricePrefix"),
        "bedrooms": raw.get("bedrooms"),
        "propertyName": property_name,
        "street": street,
        "town": town,
        "postcode": postcode,
        "altReference": raw.get("altReference"),
        "address": address,
    }

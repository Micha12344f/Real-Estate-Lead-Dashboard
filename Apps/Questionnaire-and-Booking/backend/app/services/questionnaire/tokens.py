"""Questionnaire tokens - port of the n8n 'Generate token' node logic.

Tokens are HMAC-signed compact strings binding contact/property so the
/questionnaire/{token} route can be validated statelessly.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def make_questionnaire_token(email: str, name: str, property_id: int, key: str) -> str:
    payload = {"e": email, "n": name, "p": property_id}
    body = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(key.encode(), body, hashlib.sha256).digest()
    return _b64u(body).decode() + "." + _b64u(sig).decode()


def verify_questionnaire_token(token: str, key: str) -> dict | None:
    """Verify + decode a token; returns payload dict or None if invalid."""
    try:
        body_part, sig_part = token.split(".", 1)
        pad = lambda s: s + "=" * (-len(s) % 4)
        body = base64.urlsafe_b64decode(pad(body_part))
        sig = base64.urlsafe_b64decode(pad(sig_part))
    except (ValueError, TypeError):
        return None
    expected = hmac.new(key.encode(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if (not isinstance(payload, dict) or not isinstance(payload.get("e"), str)
            or not isinstance(payload.get("n"), str)
            or type(payload.get("p")) is not int or payload["p"] < 0):
        return None
    return payload


async def verified_guest_token(token: str) -> dict | None:
    """Distinguish legacy guest links by verified payload, never by a mode/prefix.

    Anything else must pass the purpose-scoped, database-backed journey verifier;
    an invalid guest signature must not become an unauthenticated legacy request.
    """
    from ..shared.tokens_store import signing_key
    key = await signing_key()
    return verify_questionnaire_token(token, key)

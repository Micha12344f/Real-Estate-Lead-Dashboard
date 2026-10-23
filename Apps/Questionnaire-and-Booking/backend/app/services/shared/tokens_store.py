"""Tokens container: purpose-scoped, signed opaque links stored directly.

The signing key is resolved for each issuance/verification. Rotated keys
invalidate old links; issuance replaces legacy unsigned or stale-key rows.
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from .database import get_pool
from .contact_write_guard import writable_contact
from . import settings_store
from .config import is_sandbox


def _ttl_days(value: str | None, default: int = 365) -> int:
    """Positive token lifetime in days from the environment, else the default."""
    try:
        days = int(str(value).strip()) if value is not None and str(value).strip() else default
    except (TypeError, ValueError):
        days = default
    return days if days > 0 else default


# Long-lived applicant links; expiry is still enforced on verification.
TOKEN_TTL_DAYS = _ttl_days(os.environ.get("TVE_TOKEN_TTL_DAYS"))


async def signing_key() -> str:
    key = await settings_store.get("TVE_TOKEN_KEY") or "tve-dev-token-key"
    # A domain-separated sandbox signature can never verify a production link,
    # even if an operator accidentally supplies identical signing secrets.
    return hmac.new(key.encode(), b"tve-isolated-sandbox", hashlib.sha256).hexdigest() if is_sandbox() else key


def _new_token() -> str:
    """Create an opaque URL-safe token shared by sandbox and production."""
    return secrets.token_urlsafe(32)


def _signature(token: str, purpose: str, key: str) -> str:
    digest = hmac.new(key.encode(), f"{purpose}:{token}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _signed_token(purpose: str, key: str) -> str:
    token = _new_token()
    return token + "." + _signature(token, purpose, key)


def _valid_signature(token: str, purpose: str, key: str) -> bool:
    if not isinstance(token, str):
        return False
    opaque, separator, signature = token.partition(".")
    if (not separator or not re.fullmatch(r"[A-Za-z0-9_-]{43}", opaque)
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", signature)):
        return False
    return hmac.compare_digest(signature, _signature(opaque, purpose, key))


@asynccontextmanager
async def _existing_lock():
    yield


async def issue_token(contact_id: int, purpose: str, *, _contact_locked: bool = False) -> str:
    """Reuse a valid unexpired token; replace expired/stale links for the TTL."""
    key = await signing_key()
    async with _existing_lock() if _contact_locked else writable_contact(contact_id):
        return await _issue_token_locked(contact_id, purpose, key)


async def _issue_token_locked(contact_id: int, purpose: str, key: str) -> str:
    pool = await get_pool("tokens")
    expires_at = datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT token, expires_at FROM tokens WHERE contact_id=$1 AND purpose=$2", contact_id, purpose)
        if row:
            if (not _valid_signature(row["token"], purpose, key)
                    or row["expires_at"] <= datetime.now(timezone.utc)):
                token = _signed_token(purpose, key)
                row = await conn.fetchrow(
                    "UPDATE tokens SET token=$2, expires_at=$3, consumed_at=NULL "
                    "WHERE contact_id=$1 AND purpose=$4 RETURNING token",
                    contact_id, token, expires_at, purpose)
            return row["token"]
        token = _signed_token(purpose, key)
        row = await conn.fetchrow(
            """INSERT INTO tokens (contact_id, purpose, token, expires_at)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (contact_id, purpose) DO NOTHING RETURNING token""",
            contact_id, purpose, token, expires_at)
        if row:
            return row["token"]
        row = await conn.fetchrow("SELECT token FROM tokens WHERE contact_id=$1 AND purpose=$2", contact_id, purpose)
        return row["token"]


async def verify_token(token: str, purpose: str) -> dict | None:
    """Return the token row (contact_id, expires_at, consumed_at) or None."""
    if not token:
        return None
    key = await signing_key()
    # Previously issued opaque links remain usable with the original bootstrap
    # key. A changed vault key disables them, just as it disables old signatures.
    legacy = not is_sandbox() and "." not in token and key == (os.environ.get("TVE_TOKEN_KEY") or "tve-dev-token-key")
    if not legacy and not _valid_signature(token, purpose, key):
        return None
    pool = await get_pool("tokens")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, contact_id, expires_at, consumed_at FROM tokens WHERE token=$1 AND purpose=$2",
            token, purpose)
    if not row or row["expires_at"] <= datetime.now(timezone.utc):
        return None
    return dict(row)


async def consume_token(token_id: int, *, _contact_locked: bool = False) -> None:
    pool = await get_pool("tokens")
    async with pool.acquire() as conn:
        contact_id = await conn.fetchval("SELECT contact_id FROM tokens WHERE id=$1", token_id)
    if contact_id is None:
        return
    async with _existing_lock() if _contact_locked else writable_contact(contact_id):
        async with pool.acquire() as conn:
            await conn.execute("UPDATE tokens SET consumed_at=NOW() WHERE id=$1 AND contact_id=$2", token_id, contact_id)


async def tokens_for_contact(contact_id: int) -> list[dict]:
    """All token rows for a contact, oldest first."""
    pool = await get_pool("tokens")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, contact_id, purpose, token, expires_at, consumed_at, created_at"
            " FROM tokens WHERE contact_id=$1 ORDER BY id", contact_id)
    return [dict(row) for row in rows]


async def reissue_token(contact_id: int, purpose: str, *, _contact_locked: bool = False) -> str:
    """Issue a token, or refresh the existing row with a new TTL expiry.

    ``issue_token`` keeps a valid unexpired token untouched; a resent
    invitation must extend its life, so this upsert resets the expiry (and
    any earlier consumption).
    """
    key = await signing_key()
    async with _existing_lock() if _contact_locked else writable_contact(contact_id):
        return await _reissue_token_locked(contact_id, purpose, key)


async def _reissue_token_locked(contact_id: int, purpose: str, key: str) -> str:
    pool = await get_pool("tokens")
    expires_at = datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS)
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT token FROM tokens WHERE contact_id=$1 AND purpose=$2", contact_id, purpose)
        if existing and _valid_signature(existing["token"], purpose, key):
            token = existing["token"]
        else:
            token = _signed_token(purpose, key)
        row = await conn.fetchrow(
            """INSERT INTO tokens (contact_id, purpose, token, expires_at)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (contact_id, purpose) DO UPDATE
                    SET token = EXCLUDED.token, expires_at = EXCLUDED.expires_at, consumed_at = NULL
               RETURNING token""",
            contact_id, purpose, token, expires_at)
    return row["token"]

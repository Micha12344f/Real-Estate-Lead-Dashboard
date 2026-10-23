"""Encrypted credential vault.

A single Postgres container (``pg-settings``) holds per-key Fernet-encrypted
blobs. The module exposes an async API:

* :func:`get` / :func:`get_many` -- read a credential, transparently falling
  back to ``os.environ`` when no DB row exists.
* :func:`set` / :func:`delete` -- encrypt + upsert / delete a row.
* :func:`snapshot` -- metadata for the admin panel (never plaintext for
  secrets).
* :func:`invalidate` / :func:`invalidate_all` -- clear the in-process TTL
  cache after writes.

Fail-closed master key: if ``TVE_SETTINGS_MASTER_KEY`` is missing or invalid,
**any** write or **any** read of an existing DB row raises
``RuntimeError("vault_locked")``. Reads for keys that have no DB row still
fall through to ``os.environ`` so a fresh start behaves identically to today.

The TTL cache only stores **positive** results. Missing keys are re-checked
on every call so the bootstrap path stays fast without locking out new
writes from being observed quickly.
"""
from __future__ import annotations

import os
import re
import threading
import time
from contextvars import ContextVar
from functools import wraps
from typing import Any as _Any
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from .config import is_sandbox


# Shared control plane: the switch and dashboard auth must remain reachable;
# sandbox may borrow the live CRM credentials only for allowlisted GET reads.
_CONTROL_KEYS = frozenset({"TVE_SANDBOX_MODE", "TVE_WEBHOOK_SECRET",
                           "ACQUAINT_BASE", "ACQUAINT_API_KEY", "ACQUAINT_SITE_PREFIX"})
_store_mode: ContextVar[bool | None] = ContextVar("tve_settings_mode", default=None)


def _sandbox_store() -> bool:
    selected = _store_mode.get()
    return is_sandbox() if selected is None else selected


def _key_scope(function):
    @wraps(function)
    async def scoped(key, *args, **kwargs):
        selected = False if key in _CONTROL_KEYS else is_sandbox()
        token = _store_mode.set(selected)
        try:
            return await function(key, *args, **kwargs)
        finally:
            _store_mode.reset(token)
    return scoped


def _env_name(key: str) -> str:
    name = _SCOPE[key]["env"]
    return name.replace("TVE_", "TVE_SANDBOX_", 1) if _sandbox_store() else name


def _assert_writable(key: str) -> None:
    if is_sandbox() and key in _CONTROL_KEYS and key != "TVE_SANDBOX_MODE":
        raise RuntimeError("sandbox_control_setting_readonly")


_MASTER_KEY_ENV = "TVE_SETTINGS_MASTER_KEY"
_TTL_ENV = "TVE_SETTINGS_TTL_SECONDS"
_DEFAULT_TTL_SECONDS = 5
_MAX_TTL_SECONDS = 3600
AGENT_WEBHOOK_URL = "https://tve-n8n.hedgedge.info/webhook/tve-lead-intelligence-agent"


# --- Scope table ---------------------------------------------------------
# Single source of truth for which settings we manage. Used by `snapshot()`
# (batches 3+) and the env fallback path. Validation rules below enforce
# minimum hygiene on writes.

_SCOPE: dict[str, dict[str, _Any]] = {
    "TVE_FORMS_CONFIG": {"group": "forms", "is_secret": False, "env": "TVE_FORMS_CONFIG", "label": "Applicant forms"},
    "ACQUAINT_BASE":         {"group": "acquaint", "is_secret": False, "env": "ACQUAINT_BASE",         "label": "Acquaint base URL",     "placeholder": "https://api.integrators.acquaintcrm.co.uk"},
    "ACQUAINT_API_KEY":      {"group": "acquaint", "is_secret": True,  "env": "ACQUAINT_API_KEY",      "label": "Acquaint API key"},
    "ACQUAINT_SITE_PREFIX":  {"group": "acquaint", "is_secret": True,  "env": "ACQUAINT_SITE_PREFIX",  "label": "Acquaint site prefix"},
    "TVE_IMAP_HOST":         {"group": "imap",     "is_secret": False, "env": "TVE_IMAP_HOST",         "label": "IMAP host",              "placeholder": "imap.example.com"},
    "TVE_IMAP_PORT":         {"group": "imap",     "is_secret": False, "env": "TVE_IMAP_PORT",         "label": "IMAP port"},
    "TVE_IMAP_USERNAME":     {"group": "imap",     "is_secret": False, "env": "TVE_IMAP_USERNAME",     "label": "IMAP username"},
    "TVE_IMAP_PASSWORD":     {"group": "imap",     "is_secret": True,  "env": "TVE_IMAP_PASSWORD",     "label": "IMAP password"},
    "TVE_IMAP_MAILBOX":      {"group": "imap",     "is_secret": False, "env": "TVE_IMAP_MAILBOX",      "label": "IMAP mailbox"},
    "TVE_SMTP_HOST":         {"group": "smtp",     "is_secret": False, "env": "TVE_SMTP_HOST",         "label": "SMTP host"},
    "TVE_SMTP_PORT":         {"group": "smtp",     "is_secret": False, "env": "TVE_SMTP_PORT",         "label": "SMTP port"},
    "TVE_SMTP_USER":         {"group": "smtp",     "is_secret": False, "env": "TVE_SMTP_USER",         "label": "SMTP username"},
    "TVE_SMTP_PASSWORD":     {"group": "smtp",     "is_secret": True,  "env": "TVE_SMTP_PASSWORD",     "label": "SMTP password"},
    "TVE_ENQUIRY_FROM":      {"group": "smtp",     "is_secret": False, "env": "TVE_ENQUIRY_FROM",      "label": "From address",           "placeholder": "lettings@example.com"},
    "TVE_TOKEN_KEY":         {"group": "security", "is_secret": True,  "env": "TVE_TOKEN_KEY",         "label": "Token signing key"},
    "TVE_WEBHOOK_SECRET":    {"group": "security", "is_secret": True,  "env": "TVE_WEBHOOK_SECRET",    "label": "Webhook auth header"},
    "TVE_AGENT_N8N_WEBHOOK_URL": {"group": "agent", "is_secret": False, "env": "TVE_AGENT_N8N_WEBHOOK_URL", "label": "Agent n8n webhook URL", "placeholder": "https://tve-n8n.hedgedge.info/webhook/tve-lead-intelligence-agent"},
    "TVE_AGENT_N8N_WEBHOOK_SECRET": {"group": "agent", "is_secret": True, "env": "TVE_AGENT_N8N_WEBHOOK_SECRET", "label": "Agent n8n webhook secret"},
    "TVE_AGENT_DEFAULT_MODEL": {"group": "agent", "is_secret": False, "env": "TVE_AGENT_DEFAULT_MODEL", "label": "Default local model", "placeholder": "qwen3.5:27b"},
    "TVE_SANDBOX_MODE": {"group": "mode", "is_secret": False, "env": "TVE_SANDBOX_MODE", "label": "Sandbox mode"},
}


# --- Validation -----------------------------------------------------------

_SITE_PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PLACEHOLDER_PREFIXES = ("<set-", "<generate-", "<set-", "<your-", "<insert-")


def _looks_like_placeholder(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return True
    if stripped.startswith("<") and stripped.endswith(">"):
        return True
    return False


def _validate_port(value: str) -> str | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return "must be an integer"
    if not 1 <= port <= 65535:
        return "must be between 1 and 65535"
    return None


def validate(key: str, value: str) -> str | None:
    """Return ``None`` when the value is acceptable, otherwise a short 422 reason."""
    if key not in _SCOPE:
        return f"unknown setting: {key}"
    if not isinstance(value, str):
        return "must be a string"
    if key == "TVE_FORMS_CONFIG":
        import json
        from ..questionnaire.forms import validate as validate_forms
        if len(value) > 400000:
            return "form configuration is too large"
        try:
            validate_forms(json.loads(value))
        except (ValueError, TypeError):
            return "invalid form configuration"
        return None
    if len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "must be at most 4096 characters without control characters"
    if _looks_like_placeholder(value):
        return "must not be empty or a placeholder"
    if key == "ACQUAINT_BASE":
        try:
            parsed = urlparse(value)
        except Exception:
            return "must be a URL"
        try:
            valid_port = parsed.port is None or 1 <= parsed.port <= 65535
        except ValueError:
            valid_port = False
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or not valid_port or any(char.isspace() for char in value)):
            return "must be an HTTPS origin"
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            return "must not include a path or query"
        return None
    if key == "ACQUAINT_SITE_PREFIX":
        if not _SITE_PREFIX_RE.fullmatch(value):
            return "must match [A-Za-z0-9_-]{1,64}"
        return None
    if key in {"TVE_IMAP_PORT", "TVE_SMTP_PORT"}:
        return _validate_port(value)
    if key == "TVE_ENQUIRY_FROM":
        if not _EMAIL_RE.fullmatch(value):
            return "must be a valid email"
        return None
    if key == "TVE_AGENT_N8N_WEBHOOK_URL":
        if value != AGENT_WEBHOOK_URL:
            return "must match the trusted Agent webhook endpoint"
        return None
    if key == "TVE_AGENT_DEFAULT_MODEL" and value not in {"qwen3.5:27b", "qwen3:14b", "qwen3.6:latest"}:
        return "must be one of the allowed local models"
    if key == "TVE_SANDBOX_MODE" and value not in {"TRUE", "FALSE"}:
        return "must be TRUE or FALSE"
    return None


# --- Master key handling -------------------------------------------------

_fernet_lock = threading.Lock()
_fernet_instance: Fernet | None = None
_fernet_invalid_reason: str | None = None
_fernet_key: str | None = None


def _try_load_fernet() -> Fernet | None:
    """Return a cached Fernet or ``None`` when the key is missing/invalid.

    ``None`` here is the "vault_locked" signal. Callers translate that into
    a :class:`RuntimeError` for writes and existing-row reads only.
    """
    global _fernet_instance, _fernet_invalid_reason, _fernet_key
    raw = os.environ.get(_MASTER_KEY_ENV, "")
    if not raw:
        _fernet_instance = None
        _fernet_key = None
        _fernet_invalid_reason = "missing"
        return None
    with _fernet_lock:
        if _fernet_instance is not None and _fernet_key == raw:
            return _fernet_instance
        invalidate_all()
        try:
            _fernet_instance = Fernet(raw.encode("utf-8"))
            _fernet_key = raw
            _fernet_invalid_reason = None
            return _fernet_instance
        except (ValueError, TypeError):
            _fernet_instance = None
            _fernet_key = None
            _fernet_invalid_reason = "invalid"
            return None


def vault_unlocked() -> bool:
    """``True`` when the vault can read or write (master key provisioned + valid)."""
    return _try_load_fernet() is not None


def _require_fernet() -> Fernet:
    f = _try_load_fernet()
    if f is None:
        raise RuntimeError("vault_locked")
    return f


# --- TTL cache -----------------------------------------------------------

_cache: dict[tuple[bool, str], tuple[float, str]] = {}
_cache_lock = threading.Lock()


def _ttl_seconds() -> int:
    try:
        ttl = int(os.environ.get(_TTL_ENV, str(_DEFAULT_TTL_SECONDS)))
    except ValueError:
        ttl = _DEFAULT_TTL_SECONDS
    if ttl < 0:
        ttl = 0
    if ttl > _MAX_TTL_SECONDS:
        ttl = _MAX_TTL_SECONDS
    return ttl


def _cache_get(key: str) -> str | None:
    ttl = _ttl_seconds()
    if ttl <= 0:
        return None
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get((_sandbox_store(), key))
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= now:
            _cache.pop((_sandbox_store(), key), None)
            return None
        return value


def _cache_put(key: str, value: str) -> None:
    ttl = _ttl_seconds()
    if ttl <= 0:
        return
    with _cache_lock:
        _cache[(_sandbox_store(), key)] = (time.monotonic() + ttl, value)


def invalidate(key: str) -> None:
    with _cache_lock:
        for selected in (False, True):
            _cache.pop((selected, key), None)


def invalidate_all() -> None:
    with _cache_lock:
        _cache.clear()


# --- DB helpers ----------------------------------------------------------

def _get_pool():
    # Imported lazily so module import doesn't pull asyncpg unless needed
    # (also keeps unit tests trivial when stubbing get_pool).
    from app.services.shared import database
    return database.get_pool("settings", sandbox=_sandbox_store())


_SELECT = (
    "SELECT key, group_name, is_secret, updated_at, updated_via "
    "FROM settings WHERE key = $1"
)
_SELECT_CIPHERTEXT = (
    "SELECT ciphertext FROM settings WHERE key = $1"
)
_SELECT_ALL_GROUP = (
    "SELECT key, group_name, is_secret, updated_at, updated_via "
    "FROM settings WHERE group_name = $1 ORDER BY key"
)
_UPSERT = (
    "INSERT INTO settings (key, group_name, is_secret, ciphertext, updated_via) "
    "VALUES ($1, $2, $3, $4, $5) "
    "ON CONFLICT (key) DO UPDATE SET "
    "  group_name = EXCLUDED.group_name, "
    "  is_secret = EXCLUDED.is_secret, "
    "  ciphertext = EXCLUDED.ciphertext, "
    "  updated_at = now(), "
    "  updated_via = EXCLUDED.updated_via"
)
_DELETE = "DELETE FROM settings WHERE key = $1"


@_key_scope
async def _read_row(key: str) -> dict | None:
    try:
        pool = await _get_pool()
    except KeyError:
        # No settings DSN configured (e.g. test or CLI without pg-settings);
        # treat as "no DB row" and let the env fallback path take over.
        return None
    async with pool.acquire() as conn:
        return await conn.fetchrow(_SELECT, key)


async def _read_ciphertext(key: str) -> bytes | None:
    try:
        pool = await _get_pool()
    except KeyError:
        # No settings DSN configured (e.g. test or CLI without pg-settings);
        # treat as "no DB row" and let the env fallback path take over.
        return None
    async with pool.acquire() as conn:
        return await conn.fetchval(_SELECT_CIPHERTEXT, key)


# --- Public API ----------------------------------------------------------

@_key_scope
async def get(key: str) -> str | None:
    """Return the plaintext value for ``key`` or ``None``.

    Order: in-memory TTL cache → DB row (requires vault unlocked) → env.
    Missing-key env lookups are *not* cached so a freshly written value is
    observed quickly.
    """
    if key not in _SCOPE:
        return None

    # Never serve a previously decrypted row after the master key disappears.
    _try_load_fernet()
    cached = _cache_get(key) if vault_unlocked() else None
    if cached is not None:
        return cached

    cipher = await _read_ciphertext(key)
    if cipher is not None:
        # Existing DB row -- vault must be unlocked.
        f = _require_fernet()
        try:
            plaintext = f.decrypt(bytes(cipher)).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("vault_decrypt_failed") from exc
        _cache_put(key, plaintext)
        return plaintext

    # No DB row. Don't require a master key for this path.
    env_name = _env_name(key)
    env_value = os.environ.get(env_name, "")
    if env_value:
        return env_value
    return None


async def get_many(group: str) -> dict[str, str]:
    """Return plaintext values for every known key in ``group`` that resolves."""
    out: dict[str, str] = {}
    for key, meta in _SCOPE.items():
        if meta["group"] != group:
            continue
        value = await get(key)
        if value is not None:
            out[key] = value
    return out


@_key_scope
async def set(
    key: str,
    *,
    value: str,
    group: str,
    is_secret: bool,
    updated_via: str,
) -> None:
    """Encrypt and upsert. Raises :class:`RuntimeError("vault_locked")` when
    the master key is missing or invalid, or :class:`ValueError` for unknown
    keys / validation failures."""
    if key not in _SCOPE:
        raise ValueError(f"unknown setting: {key}")
    _assert_writable(key)
    reason = validate(key, value)
    if reason is not None:
        raise ValueError(reason)
    if group != _SCOPE[key]["group"]:
        raise ValueError(f"group mismatch for {key}")
    if is_secret != _SCOPE[key]["is_secret"]:
        raise ValueError(f"is_secret mismatch for {key}")

    f = _require_fernet()
    ciphertext = f.encrypt(value.encode("utf-8"))

    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_UPSERT, key, group, is_secret, ciphertext, updated_via)

    invalidate(key)


@_key_scope
async def delete(key: str) -> bool:
    """Remove a DB row (reverting to env). Returns ``True`` when a row was deleted."""
    if key not in _SCOPE:
        return False
    _assert_writable(key)
    _require_fernet()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(_DELETE, key)
    invalidate(key)
    # asyncpg returns "DELETE N"
    return result.split()[-1] != "0"


def _masked(value: str | None) -> str | None:
    if not value:
        return None
    return "••••" + (value[-4:] if len(value) > 4 else "")


async def snapshot() -> dict[str, dict]:
    """Return per-key info for the admin panel. Never includes plaintext for secrets."""
    by_key = {}
    for selected in ({False, True} if is_sandbox() else {False}):
        token = _store_mode.set(selected)
        try:
            try:
                pool = await _get_pool()
            except KeyError:
                rows = []
            else:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT key, group_name, is_secret, updated_at, updated_via FROM settings")
            for row in rows:
                expected = False if row["key"] in _CONTROL_KEYS else is_sandbox()
                if expected == selected:
                    by_key[row["key"]] = dict(row)
        finally:
            _store_mode.reset(token)

    out: dict[str, dict] = {}
    for key, meta in _SCOPE.items():
        row = by_key.get(key)
        selected = False if key in _CONTROL_KEYS else is_sandbox()
        env_name = meta["env"].replace("TVE_", "TVE_SANDBOX_", 1) if selected else meta["env"]
        env_value = os.environ.get(env_name, "")
        env_set = bool(env_value and not _looks_like_placeholder(env_value))
        entry: dict[str, _Any] = {
            "key": key,
            "group": meta["group"],
            "is_secret": meta["is_secret"],
            "label": meta.get("label", key),
            "env": env_name,
            "readonly": is_sandbox() and key in _CONTROL_KEYS and key != "TVE_SANDBOX_MODE",
            "environment": "sandbox" if selected else "control" if key in _CONTROL_KEYS else "production",
            "set": row is not None or env_set,
            "source": "database" if row else "environment" if env_set else "unset",
            "updated_at": row["updated_at"].isoformat() if row else None,
            "updated_via": row["updated_via"] if row else None,
        }
        if meta.get("placeholder"):
            entry["placeholder"] = meta["placeholder"]
        if meta["is_secret"]:
            plaintext = env_value if env_set else None
            if row is not None:
                try:
                    # Metadata must reflect the current row, not a worker cache.
                    invalidate(key)
                    plaintext = await get(key)
                except RuntimeError:
                    plaintext = None
            entry["masked"] = _masked(plaintext)
        else:
            if row is not None:
                try:
                    invalidate(key)
                    entry["value"] = await get(key)
                except RuntimeError:
                    entry["value"] = None
            else:
                entry["value"] = env_value if env_set else None
        out[key] = entry
    return out

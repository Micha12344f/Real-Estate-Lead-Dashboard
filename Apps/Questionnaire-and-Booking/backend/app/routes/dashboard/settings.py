"""Authenticated, write-only credential administration and bounded probes."""
from __future__ import annotations

import asyncio
import imaplib
import smtplib
import ssl

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, StrictBool, StrictStr

from ...services.shared import settings_store
from ...services.shared import runtime_mode
from ...services.shared.acquaint import AcquaintReadOnlyClient, READ_ENDPOINTS

router = APIRouter(prefix="/dashboard/settings")
GROUPS = {"acquaint": "Acquaint", "imap": "IMAP", "smtp": "SMTP", "security": "Security", "agent": "Agent"}


class SettingBody(BaseModel):
    class Config:
        extra = "forbid"

    value: StrictStr


class TestBody(BaseModel):
    class Config:
        extra = "forbid"

    values: dict[str, StrictStr] = Field(default_factory=dict)


class ModeBody(BaseModel):
    class Config:
        extra = "forbid"

    sandbox: StrictBool


def _field(key: str) -> dict:
    meta = settings_store._SCOPE.get(key)
    if meta is None:
        raise HTTPException(404, "Unknown setting.")
    return meta


def _validate(key: str, value: str) -> None:
    reason = settings_store.validate(key, value)
    if reason:
        # Reasons are fixed validation messages and never contain supplied values.
        raise HTTPException(422, reason)


def _unavailable(error: Exception) -> HTTPException:
    reason = str(error) if isinstance(error, RuntimeError) else ""
    if reason == "vault_locked":
        return HTTPException(503, {"reason": "vault_locked", "message": "Vault locked. Configure TVE_SETTINGS_MASTER_KEY in backend/.env and recreate the Python services."})
    if reason == "vault_decrypt_failed":
        return HTTPException(503, {"reason": "vault_decrypt_failed", "message": "The stored credentials could not be decrypted. Restore the original master key or re-enter the affected values."})
    if reason == "sandbox_control_setting_readonly":
        return HTTPException(409, "Shared Acquaint/auth settings are read-only in Sandbox. Switch to Production to edit them.")
    return HTTPException(503, {"reason": "settings_unavailable", "message": "The credential store is unavailable."})


def _audit(event: str, key: str | None = None, group: str | None = None, ok: bool = True) -> None:
    from ...main import record_event
    record_event("INFO" if ok else "WARNING", "backend", event, "Credential settings updated.",
                 setting_key=key, setting_group=group, operation="succeeded" if ok else "failed")


@router.get("")
async def settings():
    try:
        snapshot = await settings_store.snapshot()
    except Exception as error:
        raise _unavailable(error) from error
    return {"vault_unlocked": settings_store.vault_unlocked(), "groups": [
        {"id": group, "label": label, "fields": [field for field in snapshot.values() if field["group"] == group]}
        for group, label in GROUPS.items()
    ]}


@router.get("/mode")
async def get_mode():
    await runtime_mode.refresh()
    try:
        return await runtime_mode.describe()
    except Exception as error:
        raise _unavailable(error) from error


@router.put("/mode")
async def set_mode(body: ModeBody):
    value = "TRUE" if body.sandbox else "FALSE"
    try:
        settings_store._require_fernet()
        if body.sandbox:
            from ...services.shared.database import verify_sandbox_storage
            try:
                async with asyncio.timeout(15):
                    await verify_sandbox_storage()
            except Exception as error:
                raise HTTPException(503, {"reason": "sandbox_storage_unavailable", "message": "Sandbox databases are not ready. The current mode was preserved."}) from error
        await settings_store.set("TVE_SANDBOX_MODE", value=value, group="mode",
                                 is_secret=False, updated_via="dashboard")
        await runtime_mode.refresh()
        from ...main import record_event
        record_event("INFO", "backend", "mode_changed", "Operational mode changed.",
                     setting_key="TVE_SANDBOX_MODE", setting_group="mode", operation="succeeded")
        return await runtime_mode.describe()
    except HTTPException:
        raise
    except Exception as error:
        raise _unavailable(error) from error


@router.put("/{key}")
async def save_setting(key: str, body: SettingBody):
    meta = _field(key)
    _validate(key, body.value)
    if key == "TVE_SANDBOX_MODE":
        await set_mode(ModeBody(sandbox=body.value == "TRUE"))
        return (await settings_store.snapshot())[key]
    try:
        await settings_store.set(key, value=body.value, group=meta["group"],
                                 is_secret=meta["is_secret"], updated_via="dashboard")
        _audit("settings_saved", key, meta["group"])
        return (await settings_store.snapshot())[key]
    except Exception as error:
        raise _unavailable(error) from error


@router.delete("/{key}")
async def clear_setting(key: str):
    meta = _field(key)
    try:
        await settings_store.delete(key)
        if key == "TVE_SANDBOX_MODE":
            await runtime_mode.refresh()
        _audit("settings_cleared", key, meta["group"])
        return (await settings_store.snapshot())[key]
    except Exception as error:
        raise _unavailable(error) from error


@router.post("/sandbox/seed")
async def seed_sandbox():
    from ...services.shared.sandbox_data import seed
    return await seed()


def _required(values: dict, key: str) -> str:
    value = values.get(key, "")
    _validate(key, value)
    return value


def _imap_probe(values: dict) -> None:
    connection = imaplib.IMAP4_SSL(values["TVE_IMAP_HOST"], int(values.get("TVE_IMAP_PORT") or "993"),
                                  ssl_context=ssl.create_default_context(), timeout=5)
    try:
        connection.login(values["TVE_IMAP_USERNAME"], values["TVE_IMAP_PASSWORD"])
        status, _ = connection.select(values.get("TVE_IMAP_MAILBOX") or "INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("imap_select_failed")
    finally:
        # Avoid a second network wait after the bounded probe.
        connection.shutdown()


def _smtp_probe(values: dict) -> None:
    port = int(values.get("TVE_SMTP_PORT") or "587")
    if port == 465:
        connection = smtplib.SMTP_SSL(values["TVE_SMTP_HOST"], port, timeout=5, context=ssl.create_default_context())
    else:
        connection = smtplib.SMTP(values["TVE_SMTP_HOST"], port, timeout=5)
    try:
        code, _ = connection.ehlo()
        if code != 250:
            raise RuntimeError("smtp_helo_failed")
    finally:
        connection.close()


async def _acquaint_probe(values: dict) -> None:
    client = AcquaintReadOnlyClient(values["ACQUAINT_BASE"], values["ACQUAINT_API_KEY"],
                                    values["ACQUAINT_SITE_PREFIX"], timeout=5)
    await client.get(READ_ENDPOINTS["properties"], {"requestPropertyList.currentPage": 1, "requestPropertyList.pageSize": 1})


@router.post("/test/{group}")
async def test_connection(group: str, body: TestBody | None = None):
    if group not in {"acquaint", "imap", "smtp"}:
        raise HTTPException(404, "Unknown connection test.")
    candidates = body.values if body else {}
    for key, value in candidates.items():
        if _field(key)["group"] != group:
            raise HTTPException(422, "Candidate setting belongs to another group.")
        _validate(key, value)
    try:
        # Candidates can replace unreadable rows too: do not decrypt a row that
        # the caller has explicitly replaced for this test.
        values = {}
        for key, meta in settings_store._SCOPE.items():
            if meta["group"] == group:
                value = candidates[key] if key in candidates else await settings_store.get(key)
                if value is not None:
                    values[key] = value
    except Exception as error:
        raise _unavailable(error) from error
    required = {"acquaint": ("ACQUAINT_BASE", "ACQUAINT_API_KEY", "ACQUAINT_SITE_PREFIX"),
                "imap": ("TVE_IMAP_HOST", "TVE_IMAP_USERNAME", "TVE_IMAP_PASSWORD"),
                "smtp": ("TVE_SMTP_HOST",)}[group]
    for key in required:
        _required(values, key)
    for key, value in values.items():
        if value:
            _validate(key, value)
    try:
        async with asyncio.timeout(12):
            if group == "acquaint":
                await _acquaint_probe(values)
            elif group == "imap":
                await asyncio.to_thread(_imap_probe, values)
            else:
                await asyncio.to_thread(_smtp_probe, values)
    except Exception:
        _audit("settings_tested", group=group, ok=False)
        return {"ok": False, "message": "Connection test failed. Check the credentials, endpoint, and network availability."}
    _audit("settings_tested", group=group)
    message = {"acquaint": "Authentication and a read-only property page succeeded.",
               "imap": "Login and read-only mailbox selection succeeded.",
               "smtp": "SMTP HELO succeeded. Authentication and mail delivery were not tested; no mail was sent."}[group]
    return {"ok": True, "message": message}

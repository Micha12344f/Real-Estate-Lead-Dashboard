"""Authenticated n8n Agent proxy and read-only property contacts tool."""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from ...services.shared import acquaint, settings_store

router = APIRouter(prefix="/dashboard")
ALLOWED_MODELS = {"qwen3.5:27b", "qwen3:14b", "qwen3.6:latest"}
DEFAULT_URL = settings_store.AGENT_WEBHOOK_URL


class HistoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: StrictStr
    content: StrictStr


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: StrictStr = Field(min_length=1, max_length=12000)
    sessionId: StrictStr = Field(min_length=1, max_length=200)
    history: list[HistoryItem] = Field(default_factory=list, max_length=40)
    model: StrictStr


@router.post("/agent/chat")
async def agent_chat(body: ChatBody):
    from ...services.shared.config import is_sandbox
    if is_sandbox():
        raise HTTPException(503, "Agent tools use production data and are paused in Sandbox. Sandbox leads, properties and booking remain available.")
    if body.model not in ALLOWED_MODELS:
        raise HTTPException(422, "Choose an allowed local model.")
    if any(item.role not in {"user", "assistant"} or len(item.content) > 12000 for item in body.history):
        raise HTTPException(422, "Conversation history is invalid.")
    try:
        url = await settings_store.get("TVE_AGENT_N8N_WEBHOOK_URL") or os.getenv("TVE_AGENT_N8N_WEBHOOK_URL") or DEFAULT_URL
        secret = await settings_store.get("TVE_AGENT_N8N_WEBHOOK_SECRET") or os.getenv("TVE_AGENT_N8N_WEBHOOK_SECRET", "")
    except Exception:
        raise HTTPException(503, "Agent connection settings are temporarily unavailable.")
    if not secret or secret.startswith("<"):
        raise HTTPException(503, "Agent is unavailable: configure the Agent webhook secret in Settings.")
    if url != DEFAULT_URL:
        raise HTTPException(503, "Agent is unavailable: the configured webhook URL is not the trusted Agent endpoint.")
    payload = {"message": body.message, "sessionId": body.sessionId,
               "history": [item.model_dump() for item in body.history], "model": body.model}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=5.0), follow_redirects=False) as client:
            response = await client.post(url, json=payload, headers={"Accept": "application/json", "X-TVE-Auth": secret})
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("reply"), str):
            raise ValueError("invalid response")
        calls = data.get("toolCalls", [])
        safe_calls = []
        if isinstance(calls, list):
            for call in calls[:20]:
                if isinstance(call, dict) and isinstance(call.get("name"), str):
                    safe_calls.append({"name": call["name"][:100], "summary": str(call.get("summary", ""))[:500]})
        result = {"reply": data["reply"][:20000], "model": body.model, "toolCalls": safe_calls}
        if isinstance(data.get("traceId"), str):
            result["traceId"] = data["traceId"][:200]
        return result
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Agent is unavailable right now. Check the n8n connection and try again.")


@router.get("/properties/{property_id}/contacts")
async def property_contacts(property_id: int):
    if property_id < 1:
        raise HTTPException(422, "Property ID must be positive.")
    try:
        tenancies = await acquaint.client.list_tenancies(property_id)
        rows = []
        seen = set()
        for tenancy in tenancies:
            if not isinstance(tenancy, dict):
                continue
            contact_id = tenancy.get("contactId", tenancy.get("contact_id", tenancy.get("tenantId")))
            try:
                contact_id = int(contact_id)
            except (TypeError, ValueError):
                continue
            if contact_id in seen:
                continue
            seen.add(contact_id)
            contact = await acquaint.client.get_contact(contact_id)
            if not isinstance(contact, dict):
                contact = {}
            first = contact.get("firstName", contact.get("forename", ""))
            last = contact.get("lastName", contact.get("surname", ""))
            name = str(contact.get("name") or " ".join(str(x) for x in (first, last) if x).strip())[:200]
            rows.append({"contactId": contact_id, "name": name, "email": str(contact.get("email", contact.get("emailAddress", "")))[:320],
                         "relationshipTypeId": tenancy.get("relationshipTypeId", tenancy.get("typeId")),
                         "startDate": tenancy.get("startDate", tenancy.get("tenancyStartDate")),
                         "endDate": tenancy.get("endDate", tenancy.get("tenancyEndDate")),
                         "status": tenancy.get("status", tenancy.get("statusName"))})
        return {"propertyId": property_id, "contacts": rows}
    except Exception:
        raise HTTPException(502, "Property contacts could not be loaded from the read-only CRM connection.")

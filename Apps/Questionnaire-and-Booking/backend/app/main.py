"""Thames Valley Estates backend (n8n replacement).

FastAPI app exposing the same webhook surface the n8n workflows served:
  - POST /webhook/thames-valley-estates/questionnaire-submit
  - POST /webhook/thames-valley-estates/questionnaire-submit-guest
  - GET  /webhook/thames-valley-estates/booking-availability
  - POST /webhook/thames-valley-estates/booking-confirm
  - GET  /webhook/thames-valley-estates/booking-available-properties
  - POST /webhook/thames-valley-estates/booking-guest
Plus a scheduled job (cron) replacing the certification-monitoring workflow.

Also the authenticated lead dashboard for the internal views:
  - GET  /dashboard/leads, /dashboard/leads/{contact_id}
  - DELETE /dashboard/leads/{contact_id} (mode-bound, separately authorized in production)
  - GET  /dashboard/funnel, /dashboard/metrics
  - POST /dashboard/actions/assign-property, /dashboard/actions/resend-invitation
  - POST /dashboard/actions/viewing-decision (create/deny the Acquaint viewing)

All webhook routes require header auth (X-TVE-Auth header) per the
"TVE Webhook Auth" credential pattern, except where disabled via env.
"""
import asyncio
import os
import hmac
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from collections import deque
from threading import Lock
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from .routes.booking.availability import router as availability_router
from .routes.booking.confirm import router as confirm_router
from .routes.booking.properties import router as properties_router
from .routes.booking.guest import router as guest_router
from .routes.questionnaire.submissions import router as questionnaire_router
from .routes.dashboard.actions import router as dashboard_actions_router
from .routes.dashboard.agent import router as dashboard_agent_router
from .routes.dashboard.analytics import router as dashboard_analytics_router
from .routes.dashboard.leads import router as dashboard_leads_router
from .routes.dashboard.forms import router as dashboard_forms_router
from .routes.dashboard.settings import router as dashboard_settings_router
from .services.shared import settings_store
from .services.shared.config import is_sandbox
from .routes.shared.health import router as health_router
from .routes.shared.sandbox import router as sandbox_router
from .routes.shared.intake import router as intake_router

@asynccontextmanager
async def lifespan(_app):
    from .services.shared.runtime_mode import refresh, run_refresher
    await refresh()
    mode_task = asyncio.create_task(run_refresher())
    try:
        yield
    finally:
        mode_task.cancel()
        try:
            await mode_task
        except asyncio.CancelledError:
            pass
        from .services.shared.database import close_pool
        await close_pool()


app = FastAPI(title="TVE Backend 2.0", version="2.0.0", lifespan=lifespan)
logger = logging.getLogger("tve.sandbox")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
logger.propagate = False
_events = deque(maxlen=500)
_event_lock = Lock()


def record_event(level: str, source: str, event: str, message: str, **fields):
    """Keep only explicitly safe structured fields; never accept arbitrary payloads."""
    messages = {
        "request_exception": "Request failed unexpectedly.", "unhandled_exception": "Request failed unexpectedly.",
        "confirmation_request": "Booking confirmation request completed.", "questionnaire_request": "Questionnaire submission request completed.",
        "availability_request": "Availability integration request completed.", "properties_request": "Property integration request completed.",
        "request_complete": "HTTP request completed.", "integration_failure": "Integration request failed.",
        "integration_error": "Tenant lookup failed.", "tenant_resolution": "Tenant resolution completed.",
        "email_simulated": "Sandbox email simulated; no message delivered.",
        "settings_saved": "Credential override saved.",
        "settings_cleared": "Credential override cleared; bootstrap value restored.",
        "settings_tested": "Credential connection test completed.",
        "mode_changed": "Operational mode changed.",
    }
    if event not in messages:
        return
    if level not in {"INFO", "WARNING", "ERROR"} or source != "backend":
        return
    operation_messages = {
        ("tenant_resolution", "resolved"): "Current tenant(s) resolved.",
        ("tenant_resolution", "ambiguous"): "Current tenant resolution is ambiguous.",
        ("tenant_resolution", "undetermined"): "Current tenant could not be identified.",
        ("email_simulated", "lead_and_tenant_simulated"): "Sandbox email simulated for lead + tenant; no email delivered.",
        ("email_simulated", "lead_only_simulated"): "Sandbox email simulated for lead only; tenant email was not resolved. No email delivered.",
        ("email_simulated", "tenant_only_simulated"): "Current tenant resolved; lead email is unavailable for simulation. No email delivered.",
        ("email_simulated", "tenant_not_identified"): "Tenant not identified for simulated email; no email delivered.",
        ("email_simulated", "tenant_email_prepared"): "Email prepared/simulated — not sent",
        ("email_simulated", "lead_email_prepared"): "Lead email prepared/simulated — not sent",
        ("email_simulated", "tenant_email_skipped_no_address"): "Tenant email skipped — no tenant address.",
    }
    message = operation_messages.get((event, fields.get("operation")), messages[event])
    item = {"id": str(uuid.uuid4()), "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level, "source": source, "event": event, "message": message}
    allowed_routes = {"/debug/events", "/healthz", "/webhook/thames-valley-estates/booking-confirm", "/webhook/thames-valley-estates/questionnaire-submit", "/webhook/thames-valley-estates/booking-availability", "/webhook/thames-valley-estates/booking-available-properties", "/webhook/thames-valley-estates/booking-guest", "/webhook/thames-valley-estates/questionnaire-submit-guest"}
    enums = {"integration": {"acquaint", "email"}, "operation": {"tenant_lookup", "resolved", "ambiguous", "undetermined", "lead_and_tenant_simulated", "lead_only_simulated", "tenant_only_simulated", "tenant_not_identified", "tenant_email_prepared", "lead_email_prepared", "list_properties"}, "error_code": {"upstream_http_error", "upstream_unavailable"}, "exception_class": {"HTTPException", "TimeoutException", "ConnectError", "RuntimeError", "ValueError", "TypeError"}}
    enums["setting_key"] = set(settings_store._SCOPE)
    enums["setting_group"] = {"acquaint", "imap", "smtp", "security", "agent", "mode"}
    enums["operation"].update({"succeeded", "failed"})
    enums["operation"].add("tenant_email_skipped_no_address")
    allowed_routes.update({"/dashboard/leads/{contact_id}", "/dashboard/settings", "/dashboard/settings/{key}", "/dashboard/settings/test/{group}", "/dashboard/settings/mode", "/dashboard/agent/chat", "/dashboard/properties/{property_id}/contacts"})
    for key in ("request_id", "method", "route", "status", "duration_ms", "integration", "operation", "error_code", "exception_class", "setting_key", "setting_group"):
        value = fields.get(key)
        if key == "request_id" and isinstance(value, str) and REQUEST_ID_RE.fullmatch(value):
            item[key] = value
        elif key == "route" and value in allowed_routes:
            item[key] = value
        elif key in enums and value in enums[key]:
            item[key] = value
        elif key == "method" and value in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "OTHER"}:
            item[key] = value
        elif key in {"status", "duration_ms"} and isinstance(value, (int, float)):
            item[key] = value
    contact_ids = fields.get("contact_ids")
    if isinstance(contact_ids, list) and all(isinstance(value, int) and value > 0 for value in contact_ids):
        item["contact_ids"] = sorted(set(contact_ids))
    recipient_role = fields.get("recipient_role")
    if recipient_role in {"lead", "tenant"}:
        item["recipient_role"] = recipient_role
        contact_id = fields.get("contact_id")
        if contact_id is None or (isinstance(contact_id, int) and not isinstance(contact_id, bool) and contact_id > 0):
            item["contact_id"] = contact_id
    for key in ("tenant_count", "missing_email_count"):
        value = fields.get(key)
        if isinstance(value, int) and value >= 0:
            item[key] = value
    property_id = fields.get("property_id")
    if isinstance(property_id, int) and not isinstance(property_id, bool) and property_id > 0:
        item["property_id"] = property_id
    if fields.get("simulation_status") in {"simulated", "partial", "no_targets"}:
        item["simulation_status"] = fields["simulation_status"]
    with _event_lock:
        _events.append(item)
    logger.log(getattr(logging, level.upper(), logging.INFO), json.dumps(item, separators=(",", ":")))
REQUEST_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class RequestAuditMiddleware:
    """Pure ASGI request logging; avoids BaseHTTPMiddleware's EndOfStream masking."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = dict(scope.get("headers", [])).get(b"x-request-id", b"").decode("latin-1")
        request_id = supplied.lower() if REQUEST_ID_RE.fullmatch(supplied) else str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status = 500
        request_mode = None
        response_started = False

        async def send_with_audit(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                if request_mode:
                    headers.append((b"x-tve-mode", request_mode.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            from .services.shared import runtime_mode, config
            await runtime_mode.refresh()
            if scope.get("path") in {"/dashboard/settings/mode", "/dashboard/settings/TVE_SANDBOX_MODE"}:
                await self.app(scope, receive, send_with_audit)
            else:
                with config.mode_scope(config.is_sandbox()):
                    request_mode = "sandbox" if config.is_sandbox() else "production"
                    expected = dict(scope.get("headers", [])).get(b"x-tve-mode", b"").decode("latin-1")
                    if expected and expected != request_mode:
                        response = JSONResponse(status_code=409, content={"error": {"code": "mode_changed", "message": "The environment changed. Refresh before continuing.", "requestId": request_id}})
                        await response(scope, receive, send_with_audit)
                    else:
                        await self.app(scope, receive, send_with_audit)
        except Exception as exc:
            # The actual exception and traceback are retained; never log request data.
            record_event("ERROR", "backend", "request_exception", "Request failed unexpectedly.", request_id=request_id, exception_class=type(exc).__name__)
            if not response_started:
                response = JSONResponse(status_code=500, content={"error": {"code": "internal_error", "message": "Request failed unexpectedly.", "requestId": request_id}}, headers={"X-Request-ID": request_id})
                await response(scope, receive, send)
        finally:
            route = scope.get("route")
            # Never fall back to the raw URL path; unmatched paths can contain identifiers.
            route_path = getattr(route, "path", None) or "/unmatched"
            # Exclude the viewer's feed polling to prevent recursive audit noise.
            if route_path != "/debug/events":
                semantic_events = {
                    "/webhook/thames-valley-estates/booking-confirm": ("confirmation_request", "Booking confirmation request completed."),
                    "/webhook/thames-valley-estates/questionnaire-submit": ("questionnaire_request", "Questionnaire submission request completed."),
                    "/webhook/thames-valley-estates/booking-availability": ("availability_request", "Availability integration request completed."),
                    "/webhook/thames-valley-estates/booking-available-properties": ("properties_request", "Property integration request completed."),
                }
                event, message = semantic_events.get(route_path, ("request_complete", "HTTP request completed."))
                method = scope.get("method")
                if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}:
                    method = "OTHER"
                record_event("INFO" if status < 400 else "WARNING", "backend", event, message, request_id=request_id, method=method, route=route_path, status=status, duration_ms=round((time.perf_counter() - started) * 1000, 2))


app.add_middleware(RequestAuditMiddleware)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    rid = request.state.request_id
    return JSONResponse(status_code=422, content={"error":{"code":"validation_error","message":"Request validation failed.","requestId":rid}})


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    rid = getattr(request.state, "request_id", str(uuid.uuid4()))
    code = "unauthorized" if exc.status_code == 401 else "not_found" if exc.status_code == 404 else "method_not_allowed" if exc.status_code == 405 else "request_rejected"
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    code = str(detail.get("reason") or code)
    message = str(detail.get("message") or (exc.detail if isinstance(exc.detail, str) else "") or {401:"Authentication failed.",404:"Resource not found.",405:"Method not allowed."}.get(exc.status_code, "Request could not be processed."))
    body = {"error":{"code":code,"message":message,"requestId":rid}}
    # Safe passthrough for the already-requested viewing payload; nothing else leaks.
    if isinstance(detail.get("booked"), dict) and detail["booked"].get("status") == "acknowledged":
        body["error"]["booked"] = detail["booked"]
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    rid = getattr(request.state, "request_id", str(uuid.uuid4()))
    record_event("ERROR", "backend", "unhandled_exception", "Request failed unexpectedly.", request_id=rid, exception_class=type(exc).__name__)
    return JSONResponse(status_code=500, content={"error":{"code":"internal_error","message":"Request failed unexpectedly.","requestId":rid}})

TVE_WEBHOOK_SECRET = os.environ.get("TVE_WEBHOOK_SECRET", "sandbox-webhook-secret" if is_sandbox() else "")
AUTH_DISABLED = os.environ.get("TVE_AUTH_DISABLED", "false").lower() == "true"


async def verify_webhook_auth(request: Request) -> None:
    """Accept the current vault secret and bootstrap secret during rotation."""
    if AUTH_DISABLED:
        return
    supplied = request.headers.get("X-TVE-Auth", "")
    bootstrap = TVE_WEBHOOK_SECRET
    try:
        current = await settings_store.get("TVE_WEBHOOK_SECRET")
    except Exception:
        # Retain bootstrap access to repair a locked/unavailable vault.
        current = None
    if not current and not bootstrap:
        raise HTTPException(status_code=500, detail="Webhook auth not configured")
    if not any(secret and hmac.compare_digest(supplied.encode(), secret.encode()) for secret in (current, bootstrap)):
        raise HTTPException(status_code=401, detail="Invalid auth header")


app.include_router(health_router)

app.include_router(availability_router, dependencies=[Depends(verify_webhook_auth)])
app.include_router(confirm_router, dependencies=[Depends(verify_webhook_auth)])
app.include_router(properties_router, dependencies=[Depends(verify_webhook_auth)])
app.include_router(guest_router, dependencies=[Depends(verify_webhook_auth)])
# Questionnaire webhooks: same auth in production (mirrors n8n config)
app.include_router(questionnaire_router, dependencies=[Depends(verify_webhook_auth)])
app.include_router(sandbox_router, dependencies=[Depends(verify_webhook_auth)])
# Webhook email intake: provider POSTs raw inbound mail instead of polling IMAP.
app.include_router(intake_router, dependencies=[Depends(verify_webhook_auth)])
# Lead dashboard: same header auth as the webhooks it sits beside.
app.include_router(dashboard_leads_router, dependencies=[Depends(verify_webhook_auth)])
app.include_router(dashboard_analytics_router, dependencies=[Depends(verify_webhook_auth)])
app.include_router(dashboard_actions_router, dependencies=[Depends(verify_webhook_auth)])
app.include_router(dashboard_settings_router, dependencies=[Depends(verify_webhook_auth)])
app.include_router(dashboard_agent_router, dependencies=[Depends(verify_webhook_auth)])
app.include_router(dashboard_forms_router, dependencies=[Depends(verify_webhook_auth)])


@app.get("/debug/events", dependencies=[Depends(verify_webhook_auth)])
async def debug_events(after: str = ""):
    """Safe in-memory feed for the loopback-only Sandbox viewer; no request data."""
    with _event_lock:
        events = list(_events)
    if after:
        indexes = [i for i, event in enumerate(events) if event["id"] == after]
        events = events[indexes[-1] + 1:] if indexes else events
    return {"events": events[-100:]}

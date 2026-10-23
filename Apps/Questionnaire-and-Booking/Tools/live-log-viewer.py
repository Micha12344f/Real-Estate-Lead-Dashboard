"""Loopback-only, privacy-filtered Sandbox event viewer (stdlib only)."""
import json
import os
import threading
import time
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 3003
BACKEND = "http://127.0.0.1:8001/debug/events"
EVENTS = deque(maxlen=500)
LOCK = threading.Lock()
LAST_BACKEND_ID = ""
FRONTEND_EVENTS = {
    "click": "User interaction.", "change": "Form selection changed.", "submit": "Form submitted.",
    "fetch_start": "Frontend request started.", "fetch_end": "Frontend request completed.",
    "fetch_error": "Frontend request failed.", "page_load": "Page loaded.",
    "api_start": "Booking API request started.", "api_end": "Booking API request completed.",
    "api_error": "Booking API request failed.",
}
FRONTEND_ROUTES = {
    "/booking/api/available-properties": "/booking-available-properties",
    "/booking/api/booking-guest": "/booking-guest",
    "/booking/api/availability": "/booking-availability",
    "/booking/api/confirm": "/booking-confirm",
    "/questionnaire/api/questionnaire-submit": "/questionnaire-submit",
}
BACKEND_EVENTS = {
    "request_exception": ("ERROR", "Request failed unexpectedly."),
    "unhandled_exception": ("ERROR", "Request failed unexpectedly."),
    "confirmation_request": ("INFO", "Booking confirmation request completed."),
    "questionnaire_request": ("INFO", "Questionnaire submission request completed."),
    "availability_request": ("INFO", "Availability integration request completed."),
    "properties_request": ("INFO", "Property integration request completed."),
    "request_complete": ("INFO", "HTTP request completed."),
    "integration_failure": ("WARNING", "Integration request failed."),
    "integration_error": ("ERROR", "Tenant lookup failed."),
}
SAFE_ROUTES = {"/healthz", "/debug/events", "/webhook/thames-valley-estates/booking-confirm",
               "/webhook/thames-valley-estates/questionnaire-submit", "/webhook/thames-valley-estates/booking-availability",
               "/webhook/thames-valley-estates/booking-available-properties", "/webhook/thames-valley-estates/booking-guest",
               "/webhook/thames-valley-estates/questionnaire-submit-guest"}


def safe_frontend_event(data):
    """Ignore all client-provided text except a fixed event enum."""
    kind = data.get("event")
    if kind not in FRONTEND_EVENTS:
        return None
    item = {"id": str(uuid.uuid4()), "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": "ERROR" if kind in {"fetch_error", "api_error"} else "INFO", "source": "frontend",
            "event": kind, "message": FRONTEND_EVENTS[kind]}
    route = data.get("route")
    if kind.startswith("api_") and route in FRONTEND_ROUTES:
        item["route"] = route
    return item


def safe_backend_event(event):
    """Rebuild a backend record from fixed labels and typed, constrained metadata."""
    kind = event.get("event")
    if kind == "tenant_resolution":
        op = event.get("operation")
        labels = {"resolved": "Current tenant(s) resolved.", "ambiguous": "Current tenant resolution is ambiguous.",
                  "undetermined": "Current tenant could not be identified."}
        level, message = "INFO", labels.get(op, "Current tenant resolution completed.")
    elif kind == "email_simulated":
        op = event.get("operation")
        labels = {"tenant_email_prepared": "Email prepared/simulated — not sent",
                  "lead_and_tenant_simulated": "Sandbox email simulated for lead + tenant; no email delivered.",
                  "lead_only_simulated": "Sandbox email simulated for lead only; tenant email was not resolved. No email delivered.",
                  "tenant_only_simulated": "Current tenant resolved; lead email is unavailable for simulation. No email delivered.",
                  "tenant_not_identified": "Tenant not identified for simulated email; no email delivered.",
                  "lead_email_prepared": "Lead email prepared/simulated — not sent"}
        level, message = "INFO", labels.get(op, "Sandbox email simulated; no email delivered.")
    elif kind in BACKEND_EVENTS:
        level, message = BACKEND_EVENTS[kind]
    else:
        return None
    item = {"id": str(uuid.uuid4()), "timestamp": datetime.now(timezone.utc).isoformat(), "level": level,
            "source": "backend", "event": kind, "message": message}
    route = event.get("route")
    if route in SAFE_ROUTES:
        item["route"] = route
    method = event.get("method")
    if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "OTHER"}:
        item["method"] = method
    for key, low, high in (("status", 100, 599), ("duration_ms", 0, 3600000)):
        value = event.get(key)
        if isinstance(value, (int, float)) and low <= value <= high:
            item[key] = value
    rid = event.get("request_id")
    if isinstance(rid, str) and len(rid) == 36 and all(c in "0123456789abcdef-" for c in rid.lower()):
        item["request_id"] = rid
    for key, values in {"integration": {"acquaint", "email"}, "operation": {"tenant_lookup", "resolved", "ambiguous", "undetermined", "lead_and_tenant_simulated", "lead_only_simulated", "tenant_only_simulated", "tenant_not_identified", "tenant_email_prepared", "lead_email_prepared", "list_properties"}, "error_code": {"upstream_http_error", "upstream_unavailable"}, "exception_class": {"HTTPException", "TimeoutException", "ConnectError", "RuntimeError", "ValueError", "TypeError"}}.items():
        if event.get(key) in values:
            item[key] = event[key]
    ids = event.get("contact_ids")
    if isinstance(ids, list) and all(isinstance(value, int) and value > 0 for value in ids):
        item["contact_ids"] = sorted(set(ids))
    role = event.get("recipient_role")
    if role in {"lead", "tenant"}:
        item["recipient_role"] = role
        contact_id = event.get("contact_id")
        if contact_id is None or (isinstance(contact_id, int) and not isinstance(contact_id, bool) and contact_id > 0):
            item["contact_id"] = contact_id
    for key in ("tenant_count", "missing_email_count"):
        value = event.get(key)
        if isinstance(value, int) and value >= 0:
            item[key] = value
    property_id = event.get("property_id")
    if isinstance(property_id, int) and not isinstance(property_id, bool) and property_id > 0:
        item["property_id"] = property_id
    if event.get("simulation_status") in {"simulated", "partial", "no_targets"}:
        item["simulation_status"] = event["simulation_status"]
    details = []
    if "tenant_count" in item:
        details.append(f"tenants={item['tenant_count']}")
    if item.get("missing_email_count"):
        details.append(f"missing_email={item['missing_email_count']}")
    if "simulation_status" in item:
        details.append(f"simulation={item['simulation_status']}")
    if "contact_ids" in item:
        details.append("Contact IDs=" + ",".join(str(value) for value in item["contact_ids"]))
    if details:
        item["message"] += " (" + "; ".join(details) + ")"
    return item


def poll_backend():
    global LAST_BACKEND_ID
    seen = set()
    while True:
        try:
            url = BACKEND + ("?after=" + LAST_BACKEND_ID if LAST_BACKEND_ID else "")
            request = urllib.request.Request(url, headers={"X-TVE-Auth": os.environ.get("TVE_WEBHOOK_SECRET", "sandbox-webhook-secret")})
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.load(response)
            for event in payload.get("events", []):
                # Backend's event endpoint is structured, but allowlist every display field here too.
                safe = safe_backend_event(event)
                event_id = event.get("id")
                if not safe or not event_id or event_id in seen:
                    continue
                with LOCK:
                    EVENTS.append(safe)
                seen.add(event_id)
                LAST_BACKEND_ID = event_id
            if len(seen) > 1000:
                seen.clear()
        except Exception:
            pass
        time.sleep(0.5)


PAGE = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sandbox Live Events</title>
 <style>*{box-sizing:border-box}body{margin:0;background:#0b1220;color:#e5edf7;font:14px system-ui,sans-serif}.wrap{max-width:1200px;margin:auto;padding:28px}.top{display:flex;justify-content:space-between;align-items:center;gap:16px}.sub{color:#95a4ba;margin-top:5px}.badge{color:#68e0b0}.controls{display:flex;gap:8px}button{background:#1b2b42;color:#e8f0fb;border:1px solid #344a66;border-radius:6px;padding:8px 13px;cursor:pointer}.panel{margin-top:20px;border:1px solid #26374e;border-radius:9px;overflow:hidden;background:#101a29}.head,.row{display:grid;grid-template-columns:190px 78px 100px 1fr;gap:12px;padding:11px 14px}.head{background:#172438;color:#aabbd0;font-size:12px;text-transform:uppercase}.rows{height:calc(100vh - 180px);overflow:auto}.row{border-top:1px solid #202e41;font-family:ui-monospace,monospace;font-size:12px;overflow-wrap:anywhere}.message{white-space:pre-wrap}.time{color:#a0b0c3}.level{font-weight:700}.ERROR{color:#ff7d89}.WARNING{color:#ffc66d}.INFO{color:#62d9a5}.source{color:#8abaff}.empty{padding:30px;color:#98a8bc}@media(max-width:700px){.head{display:none}.row{grid-template-columns:1fr 65px;gap:5px}.message{grid-column:1/-1}}</style></head>
  <body><main class="wrap"><div class="top"><div><h1 style="margin:0">Sandbox live events</h1><div class="sub">Loopback only · simulated email content is omitted from event logs</div></div><div><span class="badge" id="status">● Connecting</span> <span id="count"></span></div></div>
<div class="controls" style="margin-top:18px"><button id="pause">Pause</button><button id="clear">Clear view</button><label style="padding:8px"><input id="scroll" type="checkbox" checked> Auto-scroll</label></div>
<section class="panel"><div class="head"><span>Timestamp</span><span>Level</span><span>Source</span><span>Event / message</span></div><div class="rows" id="rows"><div class="empty">Waiting for safe events…</div></div></section></main>
   <script>let paused=false,items=[];const rows=document.querySelector('#rows');document.querySelector('#pause').onclick=e=>{paused=!paused;e.target.textContent=paused?'Resume':'Pause'};document.querySelector('#clear').onclick=()=>{items=[];render()};function render(){if(paused)return;rows.replaceChildren();for(const x of items){const row=document.createElement('div');row.className='row';for(const [cls,value] of [['time',new Date(x.timestamp).toLocaleTimeString()],['level',x.level],['source',x.source]]){const el=document.createElement('span');el.className=cls+(cls==='level'?' '+x.level:'');el.textContent=value;row.append(el)}const msg=document.createElement('span');msg.className='message';msg.textContent=x.event+' — '+x.message+(x.recipient_role?'\nRecipient role: '+x.recipient_role:'')+(x.recipient_role?'\n'+(x.recipient_role==='tenant'?'Tenant':'Lead')+' Contact ID: '+(x.contact_id===null?'unavailable':x.contact_id):'')+(x.contact_ids?' · Contact IDs='+x.contact_ids.join(','):'')+(x.property_id?' · Property ID='+x.property_id:'')+(x.route?' · '+(x.method?x.method+' ':'')+x.route+(x.status?' · '+x.status+' · '+x.duration_ms+' ms':''):'')+(x.request_id?' · Request ID '+x.request_id:'');row.append(msg);rows.append(row)}if(!items.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='Waiting for safe events…';rows.append(empty)}document.querySelector('#count').textContent=items.length+' events';if(document.querySelector('#scroll').checked)rows.scrollTop=rows.scrollHeight}async function poll(){try{let r=await fetch('/api/events',{cache:'no-store'});if(!r.ok)throw 0;let d=await r.json();items=d.events;document.querySelector('#status').textContent='● Connected';render()}catch{document.querySelector('#status').textContent='● Reconnecting'}}setInterval(poll,750);poll();</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            body, content_type = PAGE.encode(), "text/html; charset=utf-8"
        elif self.path == "/api/events":
            with LOCK:
                body = json.dumps({"events": list(EVENTS)}).encode()
            content_type = "application/json; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self.headers.get("Origin")
        if origin in {"http://127.0.0.1:3000", "http://localhost:3000", "http://127.0.0.1:3001", "http://localhost:3001"}:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        origin = self.headers.get("Origin")
        if self.path != "/api/events" or origin not in {"http://127.0.0.1:3000", "http://localhost:3000", "http://127.0.0.1:3001", "http://localhost:3001"}:
            self.send_error(403)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_POST(self):
        origin = self.headers.get("Origin")
        allowed_origins = {"http://127.0.0.1:3000", "http://localhost:3000", "http://127.0.0.1:3001", "http://localhost:3001"}
        if self.path != "/api/events" or (origin and origin not in allowed_origins):
            self.send_error(404)
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
            item = safe_frontend_event(json.loads(self.rfile.read(length)))
        except Exception:
            item = None
        if item:
            with LOCK:
                EVENTS.append(item)
        self.send_response(202 if item else 400)
        if origin in allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()


if __name__ == "__main__":
    threading.Thread(target=poll_backend, daemon=True).start()
    print(f"Sandbox live event viewer listening at http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()

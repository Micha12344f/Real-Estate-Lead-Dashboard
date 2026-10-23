"""Read-only backlog (container log) API.

Runs on the TVE server. Talks to a docker-socket-proxy that is scoped to
container list + logs only (no exec/create/start/stop), and exposes bounded,
read-only log access over HTTP. Requires an API key. Binds to loopback only:
it is reached through an SSH tunnel, never exposed publicly.

Endpoints:
    GET /healthz
    GET /containers
    GET /logs/{name}?tail=500&since=...&timestamps=false
    GET /logs/{name}/search?q=...&tail=2000
"""

from __future__ import annotations

import hmac
import os
import re
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query

DOCKER_API = os.environ.get("DOCKER_API", "http://socket-proxy:2375")
API_KEY = os.environ.get("BACKLOG_API_KEY", "")
MAX_TAIL = int(os.environ.get("BACKLOG_MAX_TAIL", "2000"))
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

# Defence-in-depth redaction for anything credential/PII shaped that might slip
# into a log line. Logs are not expected to contain PII; this is a safety net.
_REDACTIONS = (
    (re.compile(r"(?i)(authorization|api[_-]?key|password|secret|token)\s*[:=]\s*\S+"), r"\1=***"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{10,}"), "Bearer ***"),
    (re.compile(r"\bsk-[A-Za-z0-9._\-]{8,}"), "sk-***"),
    (re.compile(r"(?i)(postgres(?:ql)?://[^:\s]+:)[^@\s]+(@)"), r"\1***\2"),
)

app = FastAPI(title="TVE Backlog Read-Only API", version="1.0.0")


def require_key(x_api_key: str = Header(default="")) -> None:
    if not API_KEY:
        raise HTTPException(status_code=503, detail="API key is not configured")
    if not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


def _decode_docker_logs(raw: bytes) -> str:
    """Decode Docker's multiplexed log stream.

    Non-TTY containers prefix each frame with an 8-byte header
    [stream(1), 0,0,0, size(4, big-endian)]. Strip those frames so the text is
    clean; fall back to a plain decode for TTY streams.
    """
    out: list[str] = []
    i = 0
    n = len(raw)
    parsed_frame = False
    while i + 8 <= n:
        if raw[i] in (0, 1, 2) and raw[i + 1] == 0 and raw[i + 2] == 0 and raw[i + 3] == 0:
            size = int.from_bytes(raw[i + 4:i + 8], "big")
            if 0 < size <= n - (i + 8):
                out.append(raw[i + 8:i + 8 + size].decode("utf-8", "replace"))
                i += 8 + size
                parsed_frame = True
                continue
        break
    if parsed_frame and i >= n:
        return "".join(out)
    # TTY or unknown framing: decode the whole buffer.
    return raw.decode("utf-8", "replace")


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _docker_get(path: str, params: dict) -> httpx.Response:
    try:
        with httpx.Client(base_url=DOCKER_API, timeout=30) as client:
            return client.get(path, params=params)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"docker proxy unavailable: {type(exc).__name__}")


def _resolve_container(name: str) -> str:
    if not NAME_RE.match(name):
        raise HTTPException(status_code=422, detail="invalid container name")
    response = _docker_get("/containers/json", {"all": "1"})
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="container list failed")
    for item in response.json():
        names = [n.lstrip("/") for n in item.get("Names", [])]
        if name in names:
            return item["Id"]
    raise HTTPException(status_code=404, detail="container not found")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "tve-backlog-api"}


@app.get("/healthz/docker", dependencies=[Depends(require_key)])
def healthz_docker() -> dict:
    response = _docker_get("/version", {})
    return {"status": "ok" if response.status_code == 200 else "error", "docker_status": response.status_code}


@app.get("/containers", dependencies=[Depends(require_key)])
def containers() -> dict:
    response = _docker_get("/containers/json", {"all": "1"})
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="container list failed")
    rows = []
    for item in response.json():
        names = [n.lstrip("/") for n in item.get("Names", [])]
        rows.append({
            "name": names[0] if names else item.get("Id", "")[:12],
            "image": item.get("Image"),
            "state": item.get("State"),
            "status": item.get("Status"),
        })
    rows.sort(key=lambda r: r["name"])
    return {"count": len(rows), "containers": rows}


@app.get("/logs/{name}", dependencies=[Depends(require_key)])
def logs(name: str, tail: int = Query(default=500, ge=1, le=MAX_TAIL),
         since: int | None = None, timestamps: bool = False) -> dict:
    container_id = _resolve_container(name)
    params = {"stdout": "1", "stderr": "1", "tail": tail}
    if since is not None:
        params["since"] = since
    if timestamps:
        params["timestamps"] = "1"
    response = _docker_get(f"/containers/{container_id}/logs", params)
    if response.status_code not in (200, 206):
        raise HTTPException(status_code=502, detail=f"logs fetch failed ({response.status_code})")
    text = _decode_docker_logs(response.content)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    lines = text.splitlines()[-tail:]
    return {"container": name, "tail": tail, "returned": len(lines),
            "log": redact("\n".join(lines))}


@app.get("/logs/{name}/search", dependencies=[Depends(require_key)])
def search(name: str, q: str, tail: int = Query(default=MAX_TAIL, ge=1, le=MAX_TAIL)) -> dict:
    if not q or len(q) > 200:
        raise HTTPException(status_code=422, detail="query must be 1..200 chars")
    container_id = _resolve_container(name)
    params = {"stdout": "1", "stderr": "1", "tail": tail}
    response = _docker_get(f"/containers/{container_id}/logs", params)
    if response.status_code not in (200, 206):
        raise HTTPException(status_code=502, detail=f"logs fetch failed ({response.status_code})")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", _decode_docker_logs(response.content))
    needle = q.lower()
    matches = [ln for ln in text.splitlines() if needle in ln.lower()][-tail:]
    return {"container": name, "query": q, "matches": len(matches), "log": redact("\n".join(matches))}


@app.get("/")
def index() -> dict:
    return {"service": "tve-backlog-api", "generated_at": datetime.now(timezone.utc).isoformat(),
            "note": "read-only container logs; requires X-API-Key; loopback only"}

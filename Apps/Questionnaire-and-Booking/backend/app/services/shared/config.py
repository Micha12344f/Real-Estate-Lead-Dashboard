"""Pinned mode selector: isolated sandbox storage/mail, optional read-only live CRM."""
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


_runtime_sandbox: bool | None = None
_runtime_source = "environment"
_operation_mode: ContextVar[bool | None] = ContextVar("tve_operation_mode", default=None)


@contextmanager
def mode_scope(sandbox: bool):
    """Pin all dependencies for one request/job, including asyncio.to_thread calls."""
    token = _operation_mode.set(sandbox)
    try:
        yield
    finally:
        _operation_mode.reset(token)


def set_runtime_mode(sandbox: bool | None, source: str = "environment") -> None:
    """Set the process-local snapshot refreshed from the operational mode store."""
    global _runtime_sandbox, _runtime_source
    _runtime_sandbox = sandbox
    _runtime_source = source if sandbox is not None else "environment"


def environment_is_sandbox() -> bool:
    values = [os.environ[key].strip().lower() for key in ("sandbox", "SANDBOX") if key in os.environ]
    if any(value not in ("true", "false") for value in values):
        raise ValueError("sandbox must be TRUE or FALSE")
    if len(set(values)) > 1:
        raise ValueError("sandbox and SANDBOX must not have conflicting values")
    return bool(values) and values[0] == "true"


def is_sandbox() -> bool:
    """Effective mode: runtime dashboard override, otherwise bootstrap env."""
    selected = _operation_mode.get()
    if selected is not None:
        return selected
    return environment_is_sandbox() if _runtime_sandbox is None else _runtime_sandbox


def fixtures_dir() -> Path:
    return Path(os.environ.get("SANDBOX_FIXTURES_DIR", str(Path(__file__).resolve().parents[4] / "Tools" / "fixtures")))


def fixture_properties() -> list[dict]:
    # Deliberately raise on missing/invalid mock data; never try real CRM.
    return json.loads((fixtures_dir() / "properties.json").read_text(encoding="utf-8"))


MOCK_INBOX_API = "http://mock-inbox:8025"
MOCK_INBOX_SMTP_HOST = "mock-inbox"
MOCK_INBOX_SMTP_PORT = 1025
INBOX_ADDRESS = "enquiries@sandbox.invalid"


def journey_url(purpose: str) -> str:
    env = "QUESTIONNAIRE_APP_BASE_URL" if purpose == "questionnaire" else "BOOKING_APP_BASE_URL"
    local = "http://127.0.0.1:3001/questionnaire" if purpose == "questionnaire" else "http://127.0.0.1:3000/booking"
    live = "https://thamesvalley-estate-agents.vercel.app/questionnaire" if purpose == "questionnaire" else "https://thamesvalley-estate-agents-booking.vercel.app/booking"
    # Hosted frontends can serve either environment. Explicit sandbox URLs are
    # optional; token signatures and storage, not a public URL, define isolation.
    if is_sandbox():
        return os.environ.get("SANDBOX_" + env, os.environ.get(env, local)).rstrip("/")
    return os.environ.get(env, live).rstrip("/")


QUESTIONNAIRE_URL = os.environ.get(
    "QUESTIONNAIRE_APP_BASE_URL",
    "http://127.0.0.1:3001/questionnaire" if is_sandbox()
    else "https://thamesvalley-estate-agents.vercel.app/questionnaire",
).rstrip("/")
BOOKING_URL = os.environ.get(
    "BOOKING_APP_BASE_URL",
    "http://127.0.0.1:3000/booking" if is_sandbox()
    else "https://thamesvalley-estate-agents-booking.vercel.app/booking",
).rstrip("/")


def apply_runtime_urls() -> None:
    """Re-evaluate mode-dependent default URLs after an operational toggle."""
    global QUESTIONNAIRE_URL, BOOKING_URL
    QUESTIONNAIRE_URL = os.environ.get(
        "QUESTIONNAIRE_APP_BASE_URL",
        "http://127.0.0.1:3001/questionnaire" if is_sandbox()
        else "https://thamesvalley-estate-agents.vercel.app/questionnaire",
    ).rstrip("/")
    BOOKING_URL = os.environ.get(
        "BOOKING_APP_BASE_URL",
        "http://127.0.0.1:3000/booking" if is_sandbox()
        else "https://thamesvalley-estate-agents-booking.vercel.app/booking",
    ).rstrip("/")
    import sys
    for module_name, name, value in (
        ("app.services.enquiries.followup", "QUESTIONNAIRE_URL", QUESTIONNAIRE_URL),
        ("app.services.enquiries.followup", "BOOKING_URL", BOOKING_URL),
        ("app.services.enquiries.simulator", "QUESTIONNAIRE_URL", QUESTIONNAIRE_URL),
        ("app.services.enquiries.simulator", "BOOKING_URL", BOOKING_URL),
        ("app.services.enquiries.journey", "BOOKING_URL", BOOKING_URL),
        ("app.services.dashboard.readmodel", "QUESTIONNAIRE_URL", QUESTIONNAIRE_URL),
        ("app.services.dashboard.readmodel", "BOOKING_URL", BOOKING_URL),
        ("app.routes.dashboard.actions", "QUESTIONNAIRE_URL", QUESTIONNAIRE_URL),
    ):
        module = sys.modules.get(module_name)
        if module is not None:
            setattr(module, name, value)

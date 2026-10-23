"""Acquaint reads and explicit viewing writes; Sandbox writes are simulated."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
from .config import is_sandbox, fixture_properties
from . import settings_store


READ_ENDPOINTS = {
    "appointments": "/v1/Appointments/list",
    "appointment_types": "/v1/appointmenttypes/list",
    "properties": "/v1/Properties/list",
    "certificates": "/v1/Certificates/list",
    "tenancies": "/v1/Tenancies/list",
    "contacts": "/v1/Contacts/list",
}
# Explicit CRM writes needed to approve a viewing. Generic mutations remain
# blocked; a missing applicant is created only through the dedicated method.
WRITE_ENDPOINTS = {
    "appointments": "/v1/Appointments",
    "lettings_applicants": "/v1/Contacts/LettingsApplicant",
}
_CONTACT_PATH = re.compile(r"^/v1/Contacts/[1-9][0-9]*$")
_MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class AcquaintConfigurationError(RuntimeError):
    """Raised before any network access when live configuration is unsafe."""


class AcquaintReadOnlyError(RuntimeError):
    """Raised when code attempts a non-read operation or unknown path."""


class AcquaintWriteError(RuntimeError):
    """Raised when an allowlisted CRM write is rejected or fails."""


class AcquaintContactError(AcquaintWriteError):
    """The applicant cannot be identified safely for a viewing."""


def _list_from_response(data: Any, keys: tuple[str, ...]) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, Mapping):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


class AcquaintReadOnlyClient:
    """Read-only client for one credential snapshot, with an auth token cache."""

    is_mock = False

    def __init__(
        self,
        base_url: str,
        api_key: str,
        site_prefix: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = self._validate_base_url(base_url)
        self.api_key = self._validate_value(api_key, "ACQUAINT_API_KEY")
        self.site_prefix = self._validate_site_prefix(site_prefix)
        self._timeout = timeout
        self._transport = transport
        self._token: str | None = None
        self._token_expires_at = 0.0

    async def _get_token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._token_expires_at:
            return self._token
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as http:
            response = await http.post(
                f"{self.base_url}/v1/auth",
                json={"sitePrefix": self.site_prefix, "apiKey": self.api_key},
                headers={"Accept": "application/json"},
            )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Acquaint authentication returned invalid JSON") from exc
        token = payload.get("token") if isinstance(payload, Mapping) else None
        if not isinstance(token, str) or not token:
            raise RuntimeError("Acquaint authentication response did not contain a token")
        try:
            lifetime = max(0, int(payload.get("expiresIn", 3600)))
        except (TypeError, ValueError):
            lifetime = 3600
        self._token = token
        self._token_expires_at = now + max(0, lifetime - min(60, lifetime * 0.1))
        return token

    @staticmethod
    def _validate_value(value: str, name: str) -> str:
        value = str(value or "").strip()
        if not value or value.startswith("<"):
            raise AcquaintConfigurationError(f"{name} is required for Acquaint access")
        return value

    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        value = cls._validate_value(value, "ACQUAINT_BASE").rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise AcquaintConfigurationError("ACQUAINT_BASE must be an HTTPS origin")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise AcquaintConfigurationError("ACQUAINT_BASE must not include a path or query")
        return value

    @staticmethod
    def _validate_site_prefix(value: str) -> str:
        value = str(value or "").strip()
        if not value or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
            raise AcquaintConfigurationError("ACQUAINT_SITE_PREFIX is invalid")
        return value

    @staticmethod
    def _assert_allowed_path(path: str) -> None:
        if not isinstance(path, str):
            raise AcquaintReadOnlyError("Acquaint path is not allowlisted")
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise AcquaintReadOnlyError("Acquaint path must be a relative allowlisted path")
        if path in READ_ENDPOINTS.values() or _CONTACT_PATH.fullmatch(path):
            return
        raise AcquaintReadOnlyError("Acquaint path is not allowlisted")

    @staticmethod
    def _assert_write_path(path: str) -> None:
        if not isinstance(path, str):
            raise AcquaintWriteError("Acquaint write path is not allowlisted")
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise AcquaintWriteError("Acquaint write path must be a relative allowlisted path")
        if path not in WRITE_ENDPOINTS.values():
            raise AcquaintWriteError("Acquaint write path is not allowlisted")

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
        }

    async def request(self, method: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """Perform one allowlisted GET; reject everything before opening transport."""
        method = str(method or "").upper()
        if method != "GET":
            if method in _MUTATION_METHODS:
                raise AcquaintReadOnlyError(f"Acquaint {method} is blocked in live-readonly mode")
            raise AcquaintReadOnlyError("Only GET is allowed in live-readonly mode")
        self._assert_allowed_path(path)
        url = f"{self.base_url}{path}"
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as http:
            response = await http.get(url, params=params, headers={**self._headers(), "Authorization": f"Bearer {token}"})
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError("Acquaint read returned invalid JSON") from exc

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, payload: dict | None = None) -> Any:
        raise AcquaintReadOnlyError("Acquaint POST is blocked in live-readonly mode")

    async def put(self, path: str, payload: dict | None = None) -> Any:
        raise AcquaintReadOnlyError("Acquaint PUT is blocked in live-readonly mode")

    async def patch(self, path: str, payload: dict | None = None) -> Any:
        raise AcquaintReadOnlyError("Acquaint PATCH is blocked in live-readonly mode")

    async def delete(self, path: str) -> Any:
        raise AcquaintReadOnlyError("Acquaint DELETE is blocked in live-readonly mode")

    async def list_appointments(self, property_id: int) -> list:
        data = await self.get(READ_ENDPOINTS["appointments"], {"request.propertyId": property_id})
        return _list_from_response(data, ("data", "items", "results", "list", "appointments"))

    async def list_appointment_types(self) -> list:
        data = await self.get(READ_ENDPOINTS["appointment_types"], {"request.pageSize": 50})
        return _list_from_response(data, ("data", "items", "results", "list", "appointmentTypes"))

    async def get_contact(self, contact_id: int) -> dict:
        data = await self.get(f"/v1/Contacts/{int(contact_id)}")
        return data if isinstance(data, dict) else {"raw": data}

    async def list_properties(self, status_id: int | None = None) -> list:
        properties: list = []
        page = 1
        while len(properties) < 1000:
            params: dict[str, Any] = {
                "requestPropertyList.currentPage": page,
                "requestPropertyList.pageSize": min(1000, 1000 - len(properties)),
            }
            if status_id is not None:
                params["requestPropertyList.statusId"] = status_id
            response = await self.get(READ_ENDPOINTS["properties"], params)
            data = response.get("data", []) if isinstance(response, Mapping) else response
            if not isinstance(data, list):
                break
            properties.extend(data[: 1000 - len(properties)])
            if not data:
                break
            pagination = response.get("pagination", {}) if isinstance(response, Mapping) else {}
            total_pages = pagination.get("totalPages", pagination.get("totalPageCount")) if isinstance(pagination, Mapping) else None
            try:
                has_more_pages = total_pages is not None and page < int(total_pages)
            except (TypeError, ValueError):
                has_more_pages = len(data) >= params["requestPropertyList.pageSize"]
            if not has_more_pages:
                break
            page += 1
        return properties

    async def list_certificates(self) -> list:
        data = await self.get(READ_ENDPOINTS["certificates"], {"currentPage": 1, "pageSize": 100})
        return _list_from_response(data, ("data", "items", "results", "list", "certificates"))

    async def list_tenancies(self, property_id: int) -> list:
        data = await self.get(READ_ENDPOINTS["tenancies"], {"request.propertyId": property_id})
        return _list_from_response(data, ("data", "items", "results", "list", "tenancies"))

    async def create_appointment(self, body: dict) -> dict:
        """Create a viewing appointment (the one allowlisted CRM mutation).

        Deliberately separate from ``request`` so read-only GET enforcement and
        its allowlist stay intact; only the fixed collection endpoint can be
        written, and it uses the client's normal bearer auth.
        """
        return await self._create_record(WRITE_ENDPOINTS["appointments"], body)

    async def create_lettings_applicant(self, body: dict) -> dict:
        return await self._create_record(WRITE_ENDPOINTS["lettings_applicants"], body)

    async def ensure_viewing_contact(self, contact: dict) -> int:
        """Use a unique exact email match, or create the missing applicant.

        Local journey ids are not CRM ids. Never assign an unrelated contact
        from a partial match, and never create after an inconclusive lookup.
        """
        email = str(contact.get("email") or "").strip().lower()
        if not email:
            raise AcquaintContactError("The lead needs an email address before approval.")
        matches = set()
        for page in range(1, 11):
            data = await self.get(READ_ENDPOINTS["contacts"], {
                "requestContacts.email": email,
                "requestContacts.currentPage": page, "requestContacts.pageSize": 100,
            })
            rows = _list_from_response(data, ("data", "items", "contacts"))
            if not isinstance(data, list) and not (isinstance(data, Mapping) and any(
                    isinstance(data.get(key), list) for key in ("data", "items", "contacts"))):
                raise AcquaintContactError("Acquaint contact lookup returned an unreadable result.")
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                if email in {str(row.get(key) or "").strip().lower() for key in ("email", "emailAlt")}:
                    value = row.get("id")
                    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                        matches.add(value)
            pagination = data.get("pagination", {}) if isinstance(data, Mapping) else {}
            total_pages = pagination.get("totalPages", pagination.get("totalPageCount"))
            if (total_pages is not None and page >= int(total_pages)) or not rows or (total_pages is None and len(rows) < 100):
                break
        else:
            raise AcquaintContactError("Too many Acquaint matches; resolve the contact before approval.")
        if len(matches) > 1:
            raise AcquaintContactError("Multiple Acquaint contacts share this email. Resolve the duplicate contacts before approval.")
        if matches:
            return matches.pop()
        record = await self.create_lettings_applicant({
            "firstNames": contact.get("first_name") or "",
            "lastName": contact.get("last_name") or "",
            "email": email, "mobile": contact.get("phone") or "",
            "altReference": "tve-" + str(contact.get("contact_uid") or contact["id"]),
        })
        value = record.get("id")
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise AcquaintContactError("Acquaint did not confirm the applicant contact. Check the contact before retrying.")
        return value

    async def _create_record(self, path: str, body: dict) -> dict:
        self._assert_write_path(path)
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as http:
            response = await http.post(
                f"{self.base_url}{path}",
                json=dict(body or {}),
                headers={**self._headers(), "Authorization": f"Bearer {token}"},
            )
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise AcquaintWriteError("Acquaint appointment creation returned invalid JSON") from exc
        return data if isinstance(data, Mapping) else {"raw": data}

    async def create_note(self, body: dict) -> dict:
        raise AcquaintReadOnlyError("Acquaint note creation is blocked in live-readonly mode")

    async def create_task(self, body: dict) -> dict:
        raise AcquaintReadOnlyError("Acquaint task creation is blocked in live-readonly mode")


class MockAcquaintClient:
    """Deterministic fixture client with Acquaint's service-facing shapes."""

    is_mock = True

    async def ensure_viewing_contact(self, contact: dict) -> int:
        return int(contact["id"])

    async def list_appointments(self, property_id: int) -> list:
        return []

    async def list_appointment_types(self) -> list:
        return []

    async def create_appointment(self, body: dict) -> dict:
        return {"id": 0, "appointmentId": 0, "localMock": True}

    async def get_contact(self, contact_id: int) -> dict:
        return {"id": contact_id, "localMock": True}

    async def create_note(self, body: dict) -> dict:
        return {"id": 0, "localMock": True}

    async def create_task(self, body: dict) -> dict:
        return {"id": 0, "localMock": True}

    async def list_properties(self, status_id: int | None = None) -> list:
        return []

    async def list_certificates(self) -> list:
        return []

    async def list_tenancies(self, property_id: int) -> list:
        return []


class SandboxAcquaintClient(MockAcquaintClient):
    """Live read-only properties and appointments when credentials are present.

    With a read-only client, ``list_properties`` serves the live catalogue
    (the selectable statuses 0 and 1 by default, briefly cached to respect
    the provider rate limit) and ``list_appointments`` translates a fixture
    property ID to its real Acquaint ``sourcePropertyId`` before GET reads
    only. With no live client the Sandbox never contacts Acquaint: properties
    stay fixtures and appointments are empty.
    """

    _PROPERTIES_CACHE_TTL = 60.0

    def __init__(self, live_client: "AcquaintReadOnlyClient | None" = None, *,
                 dynamic: bool | None = None, transport=None) -> None:
        self._live = live_client
        self._properties_cache: dict[int | None, tuple[float, list]] = {}
        # Explicitly injected clients remain deterministic for fixtures/tests.
        self._dynamic = live_client is None if dynamic is None else dynamic
        self._live_credentials_signature: tuple[str, ...] | None = None
        self._transport = transport

    @property
    def is_live(self) -> bool:
        return self._live is not None

    async def _ensure_live(self) -> None:
        if not self._dynamic:
            return
        credentials = _credential_signature(await settings_store.get_many("acquaint"))
        if credentials == self._live_credentials_signature:
            return
        # Drop both caches before validation, including on removal or bad config.
        self._live = None
        self._properties_cache.clear()
        self._live_credentials_signature = None
        self._live = _sandbox_live_client(self._transport, credentials=credentials)
        self._live_credentials_signature = credentials

    @staticmethod
    def source_property_id(fixture_id: int) -> int | None:
        for raw in fixture_properties():
            if raw.get("id") == fixture_id:
                value = raw.get("sourcePropertyId")
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    return value
        return None

    async def list_appointments(self, property_id: int) -> list:
        await self._ensure_live()
        live = self._live
        if live is None:
            return []
        real_id = self.source_property_id(property_id)
        if real_id is None:
            if any(p["id"] == property_id for p in fixture_properties()):
                return []
            real_id = property_id
        return await live.list_appointments(real_id)

    async def list_properties(self, status_id: int | None = None) -> list:
        await self._ensure_live()
        live = self._live
        if live is None:
            return [p for p in fixture_properties() if status_id is None or p.get("statusId") == status_id]
        now = time.monotonic()
        cached = self._properties_cache.get(status_id)
        if cached is not None and now - cached[0] < self._PROPERTIES_CACHE_TTL:
            return cached[1]
        if status_id is None:
            properties: list = []
            seen: set = set()
            for live_status in (0, 1):
                for raw in await live.list_properties(live_status):
                    key = raw.get("id") if isinstance(raw, Mapping) else None
                    if key is not None:
                        if key in seen:
                            continue
                        seen.add(key)
                    properties.append(raw)
        else:
            properties = await live.list_properties(status_id)
        # A concurrent rotation must not repopulate the new credential cache.
        if self._live is live:
            self._properties_cache[status_id] = (now, properties)
        return properties


def _credential_signature(values: Mapping[str, str]) -> tuple[str, ...]:
    return tuple((values.get(key) or "").strip() for key in
                 ("ACQUAINT_BASE", "ACQUAINT_API_KEY", "ACQUAINT_SITE_PREFIX"))


def _sandbox_live_client(
    transport: httpx.AsyncBaseTransport | None = None,
    *, credentials: tuple[str, ...] | None = None,
) -> AcquaintReadOnlyClient | None:
    """Build a credential snapshot; env-only bootstrap also supports sync callers.

    Dynamic sandbox reads always pass the resolved settings snapshot here.
    """
    values = _credential_signature(os.environ) if credentials is None else credentials
    if any(not value or value.startswith("<") for value in values):
        return None
    return AcquaintReadOnlyClient(*values, transport=transport)


class ConfiguredAcquaintClient(AcquaintReadOnlyClient):
    """Resolve credentials per read, replacing the authenticated client on rotation."""

    def __init__(self, *, transport=None):
        self._configured = None
        self._credentials = None
        self._transport = transport

    async def _resolve(self) -> "AcquaintReadOnlyClient":
        credentials = _credential_signature(await settings_store.get_many("acquaint"))
        if credentials != self._credentials:
            self._configured = None
            self._credentials = None
            self._configured = AcquaintReadOnlyClient(*credentials, transport=self._transport)
            self._credentials = credentials
        return self._configured

    async def request(self, method, path, params=None):
        if str(method).upper() != "GET":
            raise AcquaintReadOnlyError("Acquaint mutations are blocked")
        self._assert_allowed_path(path)
        configured = await self._resolve()
        return await configured.request(method, path, params)

    async def create_appointment(self, body: dict) -> dict:
        configured = await self._resolve()
        return await configured.create_appointment(body)

    async def create_lettings_applicant(self, body: dict) -> dict:
        configured = await self._resolve()
        return await configured.create_lettings_applicant(body)


def make_client(*, transport: httpx.AsyncBaseTransport | None = None):
    if is_sandbox():
        try:
            bootstrap = _sandbox_live_client(transport)
        except AcquaintConfigurationError:
            # Dynamic DB values may override an invalid bootstrap environment.
            bootstrap = None
        return SandboxAcquaintClient(live_client=bootstrap,
                                    dynamic=True, transport=transport)
    return ConfiguredAcquaintClient(transport=transport)


class RuntimeModeAcquaintClient:
    """Keep the shared CRM adapter aligned when the dashboard changes mode."""

    def __init__(self) -> None:
        self._sandbox = SandboxAcquaintClient(dynamic=True)
        self._production = ConfiguredAcquaintClient()
        self._mock_override = None

    @property
    def is_mock(self) -> bool:
        return is_sandbox() if self._mock_override is None else self._mock_override

    @is_mock.setter
    def is_mock(self, value) -> None:
        self._mock_override = None if value == is_sandbox() else value

    def __getattr__(self, name):
        target = self._sandbox if is_sandbox() else self._production
        return getattr(target, name)


client = RuntimeModeAcquaintClient()

"""Named connection pools, one per database container, isolated per process."""
import asyncio
import os
from urllib.parse import urlsplit, unquote, parse_qsl

from .config import is_sandbox

_pools: dict[tuple, object] = {}
_lock = asyncio.Lock()

_DSNS = {
    "contacts": "TVE_DB_CONTACTS",
    "enquiries": "TVE_DB_ENQUIRIES",
    "responses": "TVE_DB_RESPONSES",
    "tokens": "TVE_DB_TOKENS",
    "settings": "TVE_DB_SETTINGS",
}


class SandboxStorageError(RuntimeError):
    """Sandbox storage is absent or aliases a production database."""


def _identity(dsn: str) -> tuple:
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.path.strip("/"):
        raise SandboxStorageError("invalid_sandbox_database_configuration")
    # asyncpg accepts host/database overrides in DSN query parameters. Never let
    # those defeat the database identity check (SSL/application name are harmless).
    if any(key not in {"sslmode", "application_name"} for key, _ in parse_qsl(parsed.query)):
        raise SandboxStorageError("invalid_sandbox_database_configuration")
    return parsed.hostname.lower(), parsed.port or 5432, unquote(parsed.path).strip("/")


def selected_dsn(name: str, sandbox: bool) -> str:
    env_name = _DSNS[name]
    if not sandbox:
        return os.environ[env_name]
    dsn = os.environ.get(env_name.replace("TVE_DB_", "TVE_SANDBOX_DB_"), "")
    if not dsn:
        raise SandboxStorageError("sandbox_database_not_configured")
    identity = _identity(dsn)
    for production_env in (*_DSNS.values(), "DATABASE_URL"):
        production = os.environ.get(production_env)
        if production and identity == _identity(production):
            raise SandboxStorageError("sandbox_database_matches_production")
    if identity == ("postgres", 5432, "tve"):
        raise SandboxStorageError("sandbox_database_matches_production")
    return dsn


async def verify_sandbox_storage() -> None:
    tables = {"contacts": "contacts", "enquiries": "enquiries", "responses": "questionnaire_responses",
              "tokens": "tokens", "settings": "settings"}
    for name, table in tables.items():
        pool = await get_pool(name, sandbox=True)
        async with pool.acquire() as conn:
            await conn.fetchval(f"SELECT 1 FROM {table} LIMIT 1")
            if name == "responses":
                await conn.fetchval("SELECT 1 FROM tve_submissions LIMIT 1")


async def get_pool(name: str = "contacts", *, sandbox: bool | None = None):
    selected = is_sandbox() if sandbox is None else sandbox
    # Validate even when a pool exists: changed/removed config cannot reuse it.
    dsn = selected_dsn(name, selected)
    key = (selected, name, dsn)
    async with _lock:
        pool = _pools.get(key)
        if pool is None:
            import asyncpg
            pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, command_timeout=30)
            _pools[key] = pool
        return pool


async def close_pool(name: str | None = None):
    async with _lock:
        names = [key for key in _pools if name is None or key[1] == name]
        for key in names:
            pool = _pools.pop(key, None)
            if pool is not None:
                await pool.close()

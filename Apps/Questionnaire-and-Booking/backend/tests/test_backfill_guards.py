"""Legacy backfills must not resurrect a deleted lead after schema migration."""
from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(params=["backfill_enquiries", "backfill_responses"])
def script(request, monkeypatch):
    monkeypatch.setenv("TVE_DB_CONTACTS", "contacts-test")
    monkeypatch.setenv("TVE_DB_ENQUIRIES", "enquiries-test")
    monkeypatch.setenv("TVE_DB_RESPONSES", "responses-test")
    monkeypatch.setenv("DATABASE_URL", "core-test")
    path = Path(__file__).resolve().parents[1] / "docker" / f"{request.param}.py"
    spec = importlib.util.spec_from_file_location(request.param, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Contacts:
    def __init__(self, migrated=False):
        self.migrated = migrated
        self.checks = 0
        self.contacts = {"lead@example.invalid": 9}
        self.in_transaction = False
        self.closed = False

    @asynccontextmanager
    async def transaction(self):
        self.in_transaction = True
        try:
            yield
        finally:
            self.in_transaction = False

    async def fetchval(self, sql):
        assert sql == "SELECT to_regclass('lead_delete_operations')"
        self.checks += 1
        return "lead_delete_operations" if self.migrated else None

    async def fetchrow(self, sql, email):
        assert self.in_transaction and sql == "SELECT id FROM contacts WHERE lower(email)=lower($1) FOR UPDATE"
        id_ = self.contacts.get(email.lower())
        return {"id": id_} if id_ else None

    async def close(self):
        self.closed = True


class Core:
    def __init__(self):
        self.closed = False

    async def fetch(self, sql):
        assert "sender_email" in sql
        return [{"sender_email": "lead@example.invalid", "extracted_fields": {"platform": "email"},
                 "answers": {"move_timing": "Within 1 week"}}]

    async def close(self):
        self.closed = True


class Child:
    def __init__(self, contacts):
        self.contacts = contacts
        self.written = []
        self.closed = False

    async def execute(self, sql, contact_id, *values):
        assert self.contacts.in_transaction  # lock held through child write
        assert sql.lstrip().startswith("INSERT INTO ")
        self.written.append(contact_id)

    async def close(self):
        self.closed = True


def fake_connections(module, monkeypatch, contacts):
    core, child = Core(), Child(contacts)
    connected = []

    async def connect(dsn):
        connected.append(dsn)
        return {"contacts-test": contacts, "core-test": core,
                "enquiries-test": child, "responses-test": child}[dsn]

    monkeypatch.setattr(module.asyncpg, "connect", connect)
    return core, child, connected


@pytest.mark.asyncio
async def test_migrated_backfill_refuses_before_reading_legacy_data(script, monkeypatch):
    contacts = Contacts(migrated=True)
    core, child, connected = fake_connections(script, monkeypatch, contacts)
    with pytest.raises(RuntimeError, match="disabled"):
        await script.main()
    assert connected == ["contacts-test"]
    assert contacts.closed and not child.written and not core.closed


@pytest.mark.asyncio
async def test_migration_started_during_backfill_refuses_child_write(script, monkeypatch):
    contacts = Contacts()
    core, child, _ = fake_connections(script, monkeypatch, contacts)
    original = contacts.fetchrow

    async def migrating_row(sql, email):
        row = await original(sql, email)
        contacts.migrated = True  # migration becomes visible while row is locked
        return row

    contacts.fetchrow = migrating_row
    with pytest.raises(RuntimeError, match="disabled"):
        await script.main()
    assert contacts.closed and core.closed and child.closed
    assert not child.written


@pytest.mark.asyncio
async def test_pre_migration_backfill_looks_up_current_id_under_lock(script, monkeypatch):
    contacts = Contacts()
    core, child, _ = fake_connections(script, monkeypatch, contacts)
    await script.main()
    assert child.written == [9]  # lookup at write time, not an earlier email snapshot
    assert contacts.closed and core.closed and child.closed

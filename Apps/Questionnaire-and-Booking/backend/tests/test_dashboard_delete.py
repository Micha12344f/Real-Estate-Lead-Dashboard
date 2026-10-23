"""DELETE API and independent-DB failure behaviour without opening a real DSN."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException

import app.main as main
from app.services.dashboard import deletion
from app.services.shared import config, contact_write_guard, database, runtime_mode, settings_store

OP_ID = UUID("bd2b6957-231a-4f09-9674-27d08ee8fd78")
INIT_AT = datetime.now(timezone.utc)


class FakeConnection:
    def __init__(self, store, name):
        self.store, self.name = store, name

    @asynccontextmanager
    async def transaction(self):
        self.store.attempt.set()
        async with self.store.lock:
            snapshot = deepcopy((self.store.rows["contacts"], self.store.operations))
            try:
                yield self
            except Exception:
                self.store.rows["contacts"], self.store.operations = snapshot
                raise

    async def fetchrow(self, sql, *args):
        contact_id = args[0]
        if sql == "SELECT id FROM contacts WHERE id=$1 FOR UPDATE":
            return {"id": contact_id} if contact_id in self.store.rows["contacts"] else None
        if sql == "SELECT contact_uid FROM contacts WHERE id=$1 FOR UPDATE":
            return self.store.rows["contacts"].get(contact_id)
        if sql.startswith("SELECT contact_uid, operation_id, initiated_at FROM lead_delete_operations"):
            return self.store.operations.get(contact_id)
        if sql.startswith("DELETE FROM lead_delete_operations WHERE contact_id=$1"):
            if self.store.fail_receipt:
                raise OSError("receipt cleanup unavailable")
            op = self.store.operations.get(contact_id)
            if op and (op["contact_uid"], op["operation_id"]) == args[1:]:
                del self.store.operations[contact_id]
                return {"contact_id": contact_id}
            return None
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        contact_id = args[0]
        if sql.startswith("INSERT INTO lead_delete_operations"):
            self.store.operations[contact_id] = {"contact_uid": args[1], "operation_id": args[2],
                                                 "initiated_at": args[3]}
            return
        assert sql in {"DELETE FROM tokens WHERE contact_id=$1", "DELETE FROM questionnaire_responses WHERE contact_id=$1",
                       "DELETE FROM enquiries WHERE contact_id=$1", "DELETE FROM contacts WHERE id=$1 AND contact_uid=$2"}
        if self.name == self.store.fail_on:
            raise OSError("database unavailable")
        if self.name == "contacts" and self.store.rows["contacts"].get(contact_id, {}).get("contact_uid") != args[1]:
            raise AssertionError("contact identity mismatch")
        self.store.rows[self.name].pop(contact_id, None)
        self.store.deleted.append((self.name, contact_id))

    async def fetchval(self, sql, contact_id):
        if sql == "SELECT 1 FROM lead_delete_operations WHERE contact_id=$1":
            return 1 if contact_id in self.store.operations else None
        table = {"contacts": "contacts", "enquiries": "enquiries", "responses": "questionnaire_responses", "tokens": "tokens"}[self.name]
        column = "id" if self.name == "contacts" else "contact_id"
        assert sql == f"SELECT 1 FROM {table} WHERE {column}=$1 LIMIT 1"
        if self.name == self.store.fail_verify:
            raise OSError("verification unavailable")
        return 1 if contact_id in self.store.rows[self.name] else None


class FakePool:
    def __init__(self, store, name):
        self.store, self.name = store, name

    @asynccontextmanager
    async def acquire(self):
        yield FakeConnection(self.store, self.name)


class FakeDatabases:
    def __init__(self):
        self.rows = {
            "contacts": {7: {"email": "lead@example.invalid", "contact_uid": UUID(int=7)},
                         8: {"email": "neighbour@example.invalid", "contact_uid": UUID(int=8)}},
            "enquiries": {7: {"body": "private"}, 8: {"body": "neighbour"}},
            "responses": {7: {"answer": "yes"}, 8: {"answer": "no"}},
            "tokens": {7: [11, 12], 8: [13]},
        }
        self.fail_on = self.fail_verify = None
        self.fail_receipt = False
        self.operations = {}
        self.lock = asyncio.Lock()
        self.attempt = asyncio.Event()
        self.deleted = []


@pytest.fixture
def setup_delete(monkeypatch):
    sandbox, production = FakeDatabases(), FakeDatabases()

    async def pool(name, *, sandbox):
        return FakePool((sandbox_db if sandbox else production_db), name)

    sandbox_db, production_db = sandbox, production
    monkeypatch.setattr(database, "get_pool", pool)
    async def guarded_pool(name):
        return FakePool(sandbox if config.is_sandbox() else production, name)

    monkeypatch.setattr(contact_write_guard, "get_pool", guarded_pool)
    monkeypatch.setattr(database, "selected_dsn", lambda name, sandbox: f"postgresql://db/{'sandbox_' if sandbox else ''}{name}")

    async def refresh():
        return config.is_sandbox()

    async def setting(key):
        return "read-only-shared-secret"

    monkeypatch.setattr(runtime_mode, "refresh", refresh)
    monkeypatch.setattr(settings_store, "get", setting)
    monkeypatch.setattr(main, "AUTH_DISABLED", False)
    monkeypatch.setattr(main, "TVE_WEBHOOK_SECRET", "read-only-shared-secret")
    monkeypatch.setenv("TVE_WEBHOOK_SECRET", "read-only-shared-secret")
    monkeypatch.delenv("TVE_SANDBOX_DB_CONTACTS", raising=False)
    monkeypatch.delenv("TVE_SANDBOX_DB_ENQUIRIES", raising=False)
    monkeypatch.delenv("TVE_SANDBOX_DB_RESPONSES", raising=False)
    monkeypatch.delenv("TVE_SANDBOX_DB_TOKENS", raising=False)
    config.set_runtime_mode(None)
    yield sandbox, production
    config.set_runtime_mode(None)


def request(mode, lead_id=7, *, auth="read-only-shared-secret", operation=OP_ID,
            initiated=INIT_AT, contact_uid=None):
    headers = {"X-TVE-Mode": mode, "X-TVE-Auth": auth, "X-TVE-Delete-Operation": str(operation),
               "X-TVE-Delete-Initiated-At": initiated.isoformat(),
               "X-TVE-Delete-Contact-UID": str(contact_uid or UUID(int=lead_id))}
    return TestClient(main.app).delete(f"/dashboard/leads/{lead_id}", headers=headers)


def test_sandbox_delete_only_exact_id_in_selected_databases(setup_delete, monkeypatch):
    sandbox, production = setup_delete
    monkeypatch.setenv("sandbox", "TRUE")
    before = deepcopy(production.rows)
    assert request("production").status_code == 409
    assert request("sandbox", auth="wrong").status_code == 401
    assert request("sandbox", 999).status_code == 404
    assert request("sandbox").json() == {"deleted": True, "id": 7}
    assert ("tokens", 7) in sandbox.deleted  # removes both tokens through one contact_id predicate
    assert all(set(rows) == {8} for rows in sandbox.rows.values())
    assert sandbox.rows["tokens"][8] == [13]
    assert production.rows == before
    assert sandbox.operations == {}  # no identity-bearing completed receipt
    assert request("sandbox").status_code == 404  # repeated DELETE is state-idempotent
    assert request("sandbox", operation=UUID(int=9)).status_code == 404


def test_production_delete_uses_standard_auth_and_explicit_confirmation_references(setup_delete, monkeypatch):
    sandbox, production = setup_delete
    monkeypatch.setenv("sandbox", "FALSE")
    assert request("production", auth="wrong").status_code == 401
    assert TestClient(main.app).delete("/dashboard/leads/7", headers={
        "X-TVE-Auth": "read-only-shared-secret", "X-TVE-Mode": "sandbox"}).status_code == 409
    assert TestClient(main.app).delete("/dashboard/leads/7", headers={
        "X-TVE-Auth": "read-only-shared-secret", "X-TVE-Mode": "production"}).status_code == 422
    before = deepcopy(sandbox.rows)
    assert request("production").status_code == 200
    assert all(set(rows) == {8} for rows in production.rows.values())
    assert sandbox.rows == before


def test_partial_child_failure_retry_preserves_contact_until_all_children_gone(setup_delete, monkeypatch):
    sandbox, _ = setup_delete
    monkeypatch.setenv("sandbox", "TRUE")
    sandbox.fail_on = "enquiries"
    assert request("sandbox").status_code == 503
    assert 7 in sandbox.rows["contacts"]
    assert 7 not in sandbox.rows["tokens"] and 7 not in sandbox.rows["responses"]
    assert 7 in sandbox.rows["enquiries"]
    sandbox.fail_on = None
    assert request("sandbox").status_code == 200
    assert all(7 not in rows for rows in sandbox.rows.values())


def test_verify_failure_never_reports_success_and_late_orphan_requires_investigation(setup_delete, monkeypatch):
    sandbox, _ = setup_delete
    monkeypatch.setenv("sandbox", "TRUE")
    sandbox.fail_verify = "tokens"
    assert request("sandbox").status_code == 503
    assert 7 in sandbox.rows["contacts"]  # transaction rolled back
    sandbox.fail_verify = None
    assert request("sandbox").status_code == 200
    sandbox.rows["tokens"][7] = [99]  # a late independent writer
    assert request("sandbox").status_code == 404  # no receipt means no unsafe orphan cleanup
    assert sandbox.rows["tokens"][7] == [99]
    sandbox.rows["contacts"][9] = {"email": "lead@example.invalid", "contact_uid": UUID(int=9)}
    assert request("sandbox").status_code == 404
    assert 9 in sandbox.rows["contacts"]


def test_post_commit_verification_failure_is_not_success(setup_delete, monkeypatch):
    sandbox, _ = setup_delete
    monkeypatch.setenv("sandbox", "TRUE")
    original = deletion._verify_absent
    checks = 0

    async def fail_late(pools, conn, contact_id):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise OSError("verification unavailable")
        await original(pools, conn, contact_id)

    monkeypatch.setattr(deletion, "_verify_absent", fail_late)
    assert request("sandbox").status_code == 503
    assert all(7 not in rows for rows in sandbox.rows.values())
    assert request("sandbox").status_code == 200
    assert sandbox.operations == {}
    assert request("sandbox").status_code == 404


def test_receipt_cleanup_failure_preserves_pending_recovery(setup_delete, monkeypatch):
    sandbox, _ = setup_delete
    monkeypatch.setenv("sandbox", "TRUE")
    sandbox.fail_receipt = True
    assert request("sandbox").status_code == 503
    assert all(7 not in rows for rows in sandbox.rows.values())
    assert 7 in sandbox.operations
    sandbox.fail_receipt = False
    assert request("sandbox").status_code == 200
    assert sandbox.operations == {}


def test_alias_rejected_before_writes(setup_delete, monkeypatch):
    _, production = setup_delete
    monkeypatch.setenv("sandbox", "FALSE")
    monkeypatch.setenv("TVE_SANDBOX_DB_TOKENS", "postgresql://db/contacts")
    assert request("production").status_code == 503
    assert not production.deleted


@pytest.mark.asyncio
async def test_writer_commits_before_deletion_and_late_writer_cannot_commit(setup_delete, monkeypatch):
    sandbox, _ = setup_delete
    monkeypatch.setenv("sandbox", "TRUE")
    entered, release = asyncio.Event(), asyncio.Event()

    async def writer():
        with config.mode_scope(True):
            async with contact_write_guard.writable_contact(7):
                entered.set()
                await release.wait()
                sandbox.rows["enquiries"][7] = {"body": "writer committed"}

    writer_task = asyncio.create_task(writer())
    await entered.wait()
    sandbox.attempt.clear()
    delete_task = asyncio.create_task(deletion.delete_lead(7, sandbox=True, operation_id=OP_ID,
                                                          contact_uid=UUID(int=7), initiated_at=INIT_AT))
    await sandbox.attempt.wait()  # deletion has reached the held contacts transaction
    assert not delete_task.done() and not sandbox.operations
    release.set()
    await writer_task
    await delete_task
    assert all(7 not in rows for rows in sandbox.rows.values())
    with config.mode_scope(True):
        with pytest.raises(HTTPException) as denied:
            async with contact_write_guard.writable_contact(7):
                pytest.fail("late writer reached child DB")
    assert denied.value.status_code == 404


@pytest.mark.asyncio
async def test_pending_receipt_fences_writer_and_retry_reconciles_after_contact_commit(setup_delete, monkeypatch):
    sandbox, _ = setup_delete
    monkeypatch.setenv("sandbox", "TRUE")
    sandbox.fail_on = "enquiries"
    with pytest.raises(OSError):
        await deletion.delete_lead(7, sandbox=True, operation_id=OP_ID,
                                   contact_uid=UUID(int=7), initiated_at=INIT_AT)
    with config.mode_scope(True):
        with pytest.raises(HTTPException) as denied:
            async with contact_write_guard.writable_contact(7):
                pytest.fail("pending operation allowed a writer")
    assert denied.value.status_code == 409
    sandbox.fail_on = None
    original = deletion._verify_absent
    calls = 0

    async def fail_post_commit(pools, conn, contact_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            sandbox.rows["tokens"][7] = [99]  # an unmanaged late writer
            raise OSError("verification unavailable")
        await original(pools, conn, contact_id)

    monkeypatch.setattr(deletion, "_verify_absent", fail_post_commit)
    with pytest.raises(OSError):
        await deletion.delete_lead(7, sandbox=True, operation_id=OP_ID,
                                   contact_uid=UUID(int=7), initiated_at=INIT_AT)
    assert 7 not in sandbox.rows["contacts"] and sandbox.rows["tokens"][7] == [99]
    assert sandbox.operations[7]["contact_uid"] == UUID(int=7)
    await deletion.delete_lead(7, sandbox=True, operation_id=OP_ID,
                               contact_uid=UUID(int=7), initiated_at=INIT_AT)
    assert 7 not in sandbox.rows["tokens"] and sandbox.operations == {}


@pytest.mark.asyncio
async def test_retry_refuses_reassigned_id_but_does_not_expire_pending_operation(setup_delete, monkeypatch):
    sandbox, _ = setup_delete
    monkeypatch.setenv("sandbox", "TRUE")
    sandbox.fail_on = "enquiries"
    with pytest.raises(OSError):
        await deletion.delete_lead(7, sandbox=True, operation_id=OP_ID,
                                   contact_uid=UUID(int=7), initiated_at=INIT_AT)
    sandbox.fail_on = None
    sandbox.rows["contacts"][7] = {"contact_uid": UUID(int=999), "email": "other@example.invalid"}
    before = deepcopy(sandbox.rows)
    with pytest.raises(deletion.LeadConflict):
        await deletion.delete_lead(7, sandbox=True, operation_id=OP_ID,
                                   contact_uid=UUID(int=7), initiated_at=INIT_AT)
    assert sandbox.rows == before
    old = datetime.now(timezone.utc) - timedelta(days=9)
    sandbox.operations[7]["initiated_at"] = old  # unresolved pending work survives beyond seven days
    with pytest.raises(deletion.LeadConflict):
        await deletion.delete_lead(7, sandbox=True, operation_id=OP_ID, contact_uid=UUID(int=7), initiated_at=old)
    assert sandbox.rows == before
    sandbox.rows["contacts"][7] = {"contact_uid": UUID(int=7), "email": "lead@example.invalid"}
    await deletion.delete_lead(7, sandbox=True, operation_id=OP_ID, contact_uid=UUID(int=7), initiated_at=old)
    assert all(7 not in rows for rows in sandbox.rows.values()) and not sandbox.operations


def test_completed_retry_cannot_delete_reassigned_id(setup_delete, monkeypatch):
    sandbox, _ = setup_delete
    monkeypatch.setenv("sandbox", "TRUE")
    assert request("sandbox").status_code == 200
    sandbox.rows["contacts"][7] = {"contact_uid": UUID(int=999), "email": "new@example.invalid"}
    before = deepcopy(sandbox.rows)
    assert request("sandbox").status_code == 409
    assert sandbox.rows == before and not sandbox.operations

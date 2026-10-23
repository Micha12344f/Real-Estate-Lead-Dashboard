"""Mode-local, resumable deletion of precisely one four-store journey.

Pending contacts-DB operations fence known child writers across their commits.
After verification the receipt is deleted; only unresolved work retains a
recovery marker, never a completed lead's identifying information.
"""
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

from ..shared import database

NAMES = ("contacts", "enquiries", "responses", "tokens")
CHILDREN = (("tokens", "tokens"), ("responses", "questionnaire_responses"), ("enquiries", "enquiries"))


class LeadNotFound(Exception):
    pass


class LeadConflict(Exception):
    pass


def _validate_mode_storage(sandbox: bool) -> None:
    dsns = [database.selected_dsn(name, sandbox) for name in NAMES]
    identities = [database._identity(dsn) for dsn in dsns]
    if len(set(identities)) != len(NAMES):
        raise database.SandboxStorageError("journey_databases_not_distinct")
    if not sandbox:
        for name in NAMES:
            other = os.environ.get(database._DSNS[name].replace("TVE_DB_", "TVE_SANDBOX_DB_"))
            if other and database._identity(other) in identities:
                raise database.SandboxStorageError("production_database_matches_sandbox")


async def delete_lead(contact_id: int, *, sandbox: bool, operation_id: UUID,
                      contact_uid: UUID, initiated_at: datetime) -> None:
    now = datetime.now(timezone.utc)
    if initiated_at.tzinfo is None:
        raise LeadConflict
    _validate_mode_storage(sandbox)
    pools = {name: await database.get_pool(name, sandbox=sandbox) for name in NAMES}
    async with pools["contacts"].acquire() as conn:
        async with conn.transaction():
            contact = await conn.fetchrow("SELECT contact_uid FROM contacts WHERE id=$1 FOR UPDATE", contact_id)
            operation = await conn.fetchrow(
                "SELECT contact_uid, operation_id, initiated_at "
                "FROM lead_delete_operations WHERE contact_id=$1 FOR UPDATE", contact_id)
            if operation is None:
                if contact is None:
                    raise LeadNotFound
                if (contact["contact_uid"] != contact_uid or initiated_at > now + timedelta(minutes=5)
                        or initiated_at < now - timedelta(days=7)):
                    raise LeadConflict
                await conn.execute(
                    "INSERT INTO lead_delete_operations (contact_id, contact_uid, operation_id, initiated_at) "
                    "VALUES ($1,$2,$3,$4)", contact_id, contact_uid, operation_id, initiated_at)
            else:
                if (operation["operation_id"] != operation_id or operation["initiated_at"] != initiated_at
                        or operation["contact_uid"] != contact_uid):
                    raise LeadConflict
                if contact is not None and contact["contact_uid"] != contact_uid:
                    raise LeadConflict  # a newly assigned row must never be deleted by this retry

    # Pending marker is committed before touching children. Every known writer
    # checks it while holding the contacts row through its child DB write.
    async with pools["contacts"].acquire() as conn:
        async with conn.transaction():
            contact = await conn.fetchrow("SELECT contact_uid FROM contacts WHERE id=$1 FOR UPDATE", contact_id)
            if contact is not None and contact["contact_uid"] != contact_uid:
                raise LeadConflict
            for name, table in CHILDREN:
                async with pools[name].acquire() as child:
                    await child.execute(f"DELETE FROM {table} WHERE contact_id=$1", contact_id)
            if contact is not None:
                await conn.execute("DELETE FROM contacts WHERE id=$1 AND contact_uid=$2", contact_id, contact_uid)
            await _verify_absent(pools, conn, contact_id)
    # If the final read fails after the contact commit, the pending operation
    # survives; the same operation can safely reconcile and complete on retry.
    await _verify_absent(pools, None, contact_id)
    async with pools["contacts"].acquire() as conn:
        async with conn.transaction():
            # A new row explicitly assigned this ID must not be ignored when
            # dropping the fence; keep the pending marker for investigation.
            if await conn.fetchval("SELECT 1 FROM contacts WHERE id=$1 LIMIT 1", contact_id):
                raise LeadConflict
            row = await conn.fetchrow(
                "DELETE FROM lead_delete_operations WHERE contact_id=$1 "
                "AND contact_uid=$2 AND operation_id=$3 RETURNING contact_id",
                contact_id, contact_uid, operation_id)
            if row is None:
                raise RuntimeError("lead_deletion_receipt_missing")


async def _verify_absent(pools, contact_conn, contact_id):
    for name, table, column in (
        ("contacts", "contacts", "id"),
        ("enquiries", "enquiries", "contact_id"),
        ("responses", "questionnaire_responses", "contact_id"),
        ("tokens", "tokens", "contact_id"),
    ):
        if name == "contacts" and contact_conn is not None:
            exists = await contact_conn.fetchval(f"SELECT 1 FROM {table} WHERE {column}=$1 LIMIT 1", contact_id)
        else:
            async with pools[name].acquire() as conn:
                exists = await conn.fetchval(f"SELECT 1 FROM {table} WHERE {column}=$1 LIMIT 1", contact_id)
        if exists:
            raise RuntimeError("lead_deletion_incomplete")

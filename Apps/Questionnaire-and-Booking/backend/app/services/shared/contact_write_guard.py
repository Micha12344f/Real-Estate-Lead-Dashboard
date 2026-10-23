"""Cross-container writer fence, anchored in the mode-selected contacts DB.

Every child writer holds a row lock through its child DB commit. Deletion stores
its intent under the same lock before touching any other database. Callers with
an existing contact row lock must check the operation table on that connection
and pass the private ``_contact_locked`` flag to child store functions.
"""
from contextlib import asynccontextmanager

from fastapi import HTTPException

from .database import get_pool


async def assert_writable(conn, contact_id: int) -> None:
    if await conn.fetchval("SELECT 1 FROM lead_delete_operations WHERE contact_id=$1", contact_id):
        raise HTTPException(status_code=409, detail="Contact deletion in progress")


@asynccontextmanager
async def writable_contact(contact_id: int):
    pool = await get_pool("contacts")
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT id FROM contacts WHERE id=$1 FOR UPDATE", contact_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Unknown contact")
            await assert_writable(conn, contact_id)
            yield

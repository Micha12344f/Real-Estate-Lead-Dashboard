"""Shared Discord and SMTP notification helpers."""

from __future__ import annotations

async def discord_notify(message: str) -> bool:
    return False


async def send_email(to: str, subject: str, html: str, from_addr: str | None = None) -> bool:
    return False

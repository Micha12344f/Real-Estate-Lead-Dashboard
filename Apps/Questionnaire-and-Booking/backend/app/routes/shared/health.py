"""Common health and smoke-test endpoints."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/api/v1/ping")
async def ping():
    """Used by CI smoke tests."""
    return {"ok": True, "service": "tve-backend", "version": "2.0.0"}

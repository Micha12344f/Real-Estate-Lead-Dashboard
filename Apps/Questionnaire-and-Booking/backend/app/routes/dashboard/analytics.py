"""Funnel and metrics endpoints for the authenticated dashboard."""
from __future__ import annotations

from fastapi import APIRouter

from ...services.dashboard import analytics

router = APIRouter(prefix="/dashboard")


@router.get("/funnel")
async def funnel():
    """Journey-ladder counts, conversion shares and daily volume."""
    return await analytics.funnel()


@router.get("/metrics")
async def metrics():
    """Lead-volume and journey health metrics."""
    return await analytics.metrics()
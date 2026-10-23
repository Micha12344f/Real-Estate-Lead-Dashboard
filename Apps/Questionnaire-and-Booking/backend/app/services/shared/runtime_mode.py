"""Refresh the operational sandbox override into sync service call sites."""
from __future__ import annotations

import asyncio
import logging
import os

from . import settings_store
from . import config

logger = logging.getLogger("tve.runtime-mode")
REFRESH_SECONDS = 3


async def refresh() -> bool:
    """Refresh the mode snapshot; keep the last known override on store errors."""
    try:
        value = await settings_store.get("TVE_SANDBOX_MODE")
    except Exception as error:
        logger.warning("runtime mode refresh failed error_type=%s", type(error).__name__)
        if config._runtime_sandbox is None:
            # If persisted mode cannot be read during startup, do not enable live effects.
            config.set_runtime_mode(True, "safety")
            config.apply_runtime_urls()
        return config.is_sandbox()
    if value is None:
        config.set_runtime_mode(None)
        config.apply_runtime_urls()
        return config.is_sandbox()
    if value not in {"TRUE", "FALSE"}:
        logger.error("runtime mode setting invalid; retaining current mode")
        return config.is_sandbox()
    config.set_runtime_mode(value == "TRUE", "dashboard")
    config.apply_runtime_urls()
    return value == "TRUE"


async def run_refresher() -> None:
    while True:
        await refresh()
        await asyncio.sleep(REFRESH_SECONDS)


def current() -> dict:
    """Return effective mode and source without exposing unrelated settings."""
    sandbox = config.is_sandbox()
    configured_env_mode = os.environ.get("TVE_SANDBOX_MODE", "").strip()
    environment_mode = configured_env_mode.lower() if configured_env_mode.lower() in {"true", "false"} else (
        "sandbox" if config.environment_is_sandbox() else "production"
    )
    return {
        "sandbox": sandbox,
        "mode": "sandbox" if sandbox else "production",
        "source": config._runtime_source,
        "environmentMode": "sandbox" if environment_mode in {"true", "sandbox"} else "production",
    }


async def describe() -> dict:
    """Return mode plus whether it comes from a DB override or bootstrap env."""
    try:
        row = await settings_store._read_row("TVE_SANDBOX_MODE")
    except Exception as error:
        logger.warning("runtime mode metadata failed error_type=%s", type(error).__name__)
        row = None
    result = current()
    if config._runtime_source == "safety":
        result["source"] = "safety default"
    elif row is not None:
        result["source"] = "dashboard"
    elif os.environ.get("TVE_SANDBOX_MODE"):
        result["source"] = "environment"
    else:
        result["source"] = "environment"
    return result

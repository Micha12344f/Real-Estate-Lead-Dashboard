"""Authenticated local scenario dashboard API; inaccessible outside sandbox mode."""
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from ...services.shared.config import is_sandbox, fixtures_dir
from ...services.shared.database import get_pool
from ...services.enquiries.simulator import SCENARIOS, start_scenario, dashboard_runs

router = APIRouter(prefix="/sandbox")


def require_sandbox():
    if not is_sandbox():
        raise HTTPException(404, detail="Sandbox is not enabled")


class StartBody(BaseModel):
    scenario: str = "rightmove"
    fixtureIndex: int = Field(default=0, ge=0)


@router.get("/scenarios")
async def scenarios():
    require_sandbox()
    entries = []
    for key, spec in SCENARIOS.items():
        records = json.loads((fixtures_dir() / "emails" / (spec["source"] + ".json")).read_text(encoding="utf-8"))
        entries.append({"id": key, **spec, "count": len(records)})
    return {"sandbox": True, "scenarios": entries, "mailboxUrl": "http://127.0.0.1:8025"}


@router.post("/runs")
async def start(body: StartBody):
    require_sandbox()
    return await start_scenario(await get_pool(), body.scenario, body.fixtureIndex)


@router.get("/runs")
async def runs():
    require_sandbox()
    return {"runs": await dashboard_runs(await get_pool())}
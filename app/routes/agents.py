"""Agents — admin monitors all; users see their own."""
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from app.database import pg_service as pg
from app.routes.deps import current_payload

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


def _is_admin(payload: dict) -> bool:
    return payload.get("account_type") in ("admin", "superadmin")


class AgentIn(BaseModel):
    agent_id: Optional[str] = None
    name: str
    type: Optional[str] = None
    status: Optional[str] = "inactive"
    total_runs: Optional[int] = 0
    success_rate: Optional[float] = 0
    last_run_at: Optional[str] = None


@router.get("")
async def list_agents(user_id: Optional[str] = Query(None), payload: dict = Depends(current_payload)):
    if _is_admin(payload):
        return await pg.list_agents(user_id)
    return await pg.list_agents(payload.get("user_id"))


@router.get("/{aid}")
async def get_agent(aid: str, payload: dict = Depends(current_payload)):
    a = await pg.get_agent(aid)
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not _is_admin(payload) and a.get("user_id") != payload.get("user_id"):
        raise HTTPException(status_code=403, detail="Forbidden")
    return a


@router.post("", status_code=201)
async def create_agent(body: AgentIn, payload: dict = Depends(current_payload)):
    data = body.model_dump()
    data["agent_id"] = data.get("agent_id") or f"agt-{int(time.time() * 1000)}"
    data["user_id"] = payload.get("user_id")
    lra = data.get("last_run_at")
    if lra:
        try:
            data["last_run_at"] = datetime.fromisoformat(str(lra).replace("Z", "+00:00"))
        except Exception:
            data["last_run_at"] = None
    else:
        data["last_run_at"] = None
    return await pg.upsert_agent(data)

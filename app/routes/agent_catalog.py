"""Agent catalog — admin (CRUD) + user (browse published) dashboards."""
import time
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel
from app.database import pg_service as pg
from app.routes.deps import current_payload, require_admin, require_superadmin

router = APIRouter(prefix="/api/v1/agent-catalog", tags=["agent-catalog"])


class AgentCatalogIn(BaseModel):
    name: str
    description: Optional[str] = None
    category: Optional[str] = "general"
    icon: Optional[str] = None
    tags: Optional[List[str]] = []
    status: Optional[str] = "draft"
    config: Optional[Dict[str, Any]] = {}


@router.get("")
async def list_items(status: Optional[str] = Query(None), payload: dict = Depends(current_payload)):
    return await pg.list_agent_catalog(status)


@router.get("/{aid}")
async def get_item(aid: str, payload: dict = Depends(current_payload)):
    a = await pg.get_agent_catalog(aid)
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")
    return a


@router.post("", status_code=201)
async def create_item(body: AgentCatalogIn, payload: dict = Depends(require_admin)):
    data = body.model_dump()
    data["id"] = f"agt-{int(time.time() * 1000)}"
    data["created_by"] = payload.get("user_id")
    return await pg.insert_agent_catalog(data)


@router.put("/{aid}")
async def update_item(aid: str, body: AgentCatalogIn, payload: dict = Depends(require_admin)):
    updated = await pg.update_agent_catalog(aid, body.model_dump(exclude_unset=True))
    if not updated:
        raise HTTPException(status_code=404, detail="Agent not found")
    return updated


@router.delete("/{aid}")
async def delete_item(aid: str, payload: dict = Depends(require_superadmin)):
    ok = await pg.delete_agent_catalog(aid)
    if not ok:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"deleted": True}

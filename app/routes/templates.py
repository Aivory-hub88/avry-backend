"""Automation templates — shared by admin (CRUD) and user (browse) dashboards."""
import time
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from app.database import pg_service as pg
from app.routes.deps import current_payload, require_admin, require_superadmin

router = APIRouter(prefix="/api/v1/templates", tags=["templates"])


class TemplateIn(BaseModel):
    name: str
    description: Optional[str] = None
    category: Optional[str] = "general"
    tags: Optional[List[str]] = []
    apps: Optional[List[str]] = []
    status: Optional[str] = "draft"
    workflow_json: Optional[Dict[str, Any]] = {}


@router.get("")
async def list_templates(status: Optional[str] = Query(None), payload: dict = Depends(current_payload)):
    return await pg.list_templates(status)


@router.get("/{tid}")
async def get_template(tid: str, payload: dict = Depends(current_payload)):
    t = await pg.get_template(tid)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    return t


@router.post("", status_code=201)
async def create_template(body: TemplateIn, payload: dict = Depends(require_admin)):
    data = body.model_dump()
    data["id"] = f"tpl-{int(time.time() * 1000)}"
    data["created_by"] = payload.get("user_id")
    return await pg.insert_template(data)


@router.put("/{tid}")
async def update_template(tid: str, body: TemplateIn, payload: dict = Depends(require_admin)):
    updated = await pg.update_template(tid, body.model_dump(exclude_unset=True))
    if not updated:
        raise HTTPException(status_code=404, detail="Template not found")
    return updated


@router.delete("/{tid}")
async def delete_template(tid: str, payload: dict = Depends(require_superadmin)):
    ok = await pg.delete_template(tid)
    if not ok:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"success": True, "deleted": tid}


@router.post("/{tid}/use")
async def use_template(tid: str, payload: dict = Depends(current_payload)):
    t = await pg.increment_template_uses(tid)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    return t

"""
Scraper honeypot hits — records from the frontend-nextjs trap route
(/api/internal/trap) whenever a scanner follows a hidden canary link.

Internal (frontend-facing, X-Internal-Token == HONEYPOT_INGEST_TOKEN):
    POST /api/v1/trap-hits/internal

Dashboard-facing (JWT auth, admin/superadmin only):
    GET  /api/v1/trap-hits
"""

import os
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.database.db_service import DatabaseService
from app.routes.admin_users import require_admin

router = APIRouter(prefix="/api/v1/trap-hits", tags=["trap-hits"])

db_service = DatabaseService()

COLLECTION = "trap_hits"


class TrapHitRequest(BaseModel):
    ip: str = Field(min_length=1, max_length=64)
    userAgent: str = Field(default="unknown", max_length=512)
    path: str = Field(default="/api/internal/trap", max_length=256)
    ts: Optional[str] = None


def require_internal_trap_token(x_internal_token: Optional[str] = Header(None)) -> None:
    """Shared-secret auth for the frontend-nextjs trap route."""
    expected = os.getenv("HONEYPOT_INGEST_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="Internal token not configured")
    if not x_internal_token or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/internal", dependencies=[Depends(require_internal_trap_token)])
def record_trap_hit(body: TrapHitRequest):
    """Store one honeypot hit reported by the public site's trap route."""
    now = datetime.utcnow()
    hit_id = f"{now.strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(4)}"
    record = {
        "hit_id": hit_id,
        "ip": body.ip,
        "user_agent": body.userAgent,
        "path": body.path,
        "created_at": body.ts or now.isoformat(),
    }
    db_service.save_json(COLLECTION, hit_id, record)
    return {"ok": True, "hit_id": hit_id}


@router.get("")
def list_trap_hits(limit: int = 50, admin: dict = Depends(require_admin)):
    """Newest-first trap-hit list for the admin Security & Honeypot page."""
    limit = max(1, min(limit, 200))
    records = db_service.load_all_json(COLLECTION) or []
    records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return {"hits": records[:limit], "total": len(records)}

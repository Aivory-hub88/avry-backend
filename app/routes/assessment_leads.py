"""
Free assessment leads — one record per completed public assessment on
aivory.uk/free-diagnostic, captured at the email gate that unlocks the
downloadable report cards.

Internal (frontend-facing, X-Internal-Token == LEAD_INGEST_TOKEN):
    POST /api/v1/assessment-leads/internal

Dashboard-facing (JWT auth, admin/superadmin only):
    GET  /api/v1/assessment-leads

Storage is Postgres. If the pool is down, the lead is written to the JSON file
store instead — a lead is a paid-traffic conversion and must never be dropped
because of a transient database problem.
"""

import logging
import os
import secrets
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.database.db_service import DatabaseService
from app.routes.admin_users import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/assessment-leads", tags=["assessment-leads"])

db_service = DatabaseService()

COLLECTION = "assessment_leads"

try:
    from app.database import pg_service as pg
except ImportError:  # pragma: no cover - mirrors main.py's optional PG import
    pg = None


class AssessmentLeadRequest(BaseModel):
    email: EmailStr
    companyName: Optional[str] = Field(default=None, max_length=200)
    industry: Optional[str] = Field(default=None, max_length=100)
    companySize: Optional[str] = Field(default=None, max_length=50)
    score: Optional[int] = Field(default=None, ge=0, le=100)
    maturity: Optional[str] = Field(default=None, max_length=50)
    answers: Dict[str, int] = Field(default_factory=dict)
    strengths: List[str] = Field(default_factory=list)
    blockers: List[str] = Field(default_factory=list)
    source: str = Field(default="free-assessment", max_length=50)
    # Which question set produced `answers`. 1 = the original AI-readiness set,
    # 2 = the operations set. The keys differ completely between the two, so
    # any analysis of `answers` has to filter on this first.
    questionSetVersion: int = Field(default=1, ge=1, le=99)
    ip: Optional[str] = Field(default=None, max_length=45)
    userAgent: Optional[str] = Field(default=None, max_length=512)


def require_internal_lead_token(x_internal_token: Optional[str] = Header(None)) -> None:
    """Shared-secret auth for the public site's /api/assessment-lead proxy."""
    expected = os.getenv("LEAD_INGEST_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="Internal token not configured")
    if not x_internal_token or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/internal", dependencies=[Depends(require_internal_lead_token)])
async def record_assessment_lead(body: AssessmentLeadRequest):
    """Store one completed free assessment reported by the public site."""
    now = datetime.utcnow()
    lead_id = f"lead_{now.strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(4)}"

    record = {
        "id": lead_id,
        "email": str(body.email).strip().lower(),
        "company_name": body.companyName,
        "industry": body.industry,
        "company_size": body.companySize,
        "score": body.score,
        "maturity": body.maturity,
        "answers": body.answers,
        "strengths": body.strengths[:10],
        "blockers": body.blockers[:10],
        "source": body.source,
        "question_set_version": body.questionSetVersion,
        "ip": body.ip,
        "user_agent": body.userAgent,
    }

    if pg and await pg.is_available():
        try:
            await pg.insert_assessment_lead(record)
            return {"ok": True, "id": lead_id, "storage": "postgres"}
        except Exception as e:
            logger.error(f"[!] assessment lead PG insert failed, falling back to file: {e}")

    db_service.save_json(COLLECTION, lead_id, {**record, "created_at": now.isoformat()})
    return {"ok": True, "id": lead_id, "storage": "file"}


@router.get("")
async def list_assessment_leads(
    limit: int = 100,
    offset: int = 0,
    admin: dict = Depends(require_admin),
):
    """Newest-first lead list for the admin dashboard."""
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    if pg and await pg.is_available():
        try:
            leads = await pg.list_assessment_leads(limit=limit, offset=offset)
            total = await pg.count_assessment_leads()
            return {"leads": leads, "total": total, "storage": "postgres"}
        except Exception as e:
            logger.error(f"[!] assessment lead PG read failed, falling back to file: {e}")

    records = db_service.load_all_json(COLLECTION) or []
    records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return {
        "leads": records[offset:offset + limit],
        "total": len(records),
        "storage": "file",
    }

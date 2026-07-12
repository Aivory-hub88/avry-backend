"""
Agent action log — structured records produced by deployable-agent tools.

The bridge agent gateway calls the internal endpoint whenever an agent uses an
action tool (save_lead, create_ticket, record_invoice, ...). Records are stored
per user so the dashboard can show what the agents actually did.

Internal (bridge-facing, X-Internal-Token == TELEGRAM_GATEWAY_TOKEN):
    POST /api/v1/agent-actions/internal

Dashboard-facing (JWT auth):
    GET  /api/v1/agent-actions            -> newest-first list (optional filters)
"""

import logging
import os
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.database.db_service import DatabaseService
from app.services.auth_service import AuthService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent-actions", tags=["agent-actions"])

db_service = DatabaseService()
auth_service = AuthService(db_service)

ACTIONS_COLLECTION = "agent_actions"

# What agent tools are allowed to record. Anything else is rejected so the
# collection stays queryable by the dashboard.
ACTION_TYPES = {
    "lead",          # leads_qualifier: BANT-qualified lead
    "ticket",        # customer_service: support ticket
    "escalation",    # customer_service: human handoff request
    "invoice",       # finance_invoice_ops: recorded invoice
    "anomaly",       # finance_invoice_ops: flagged anomaly
    "workflow",      # any agent: n8n workflow triggered
    "integration",   # any agent: Composio action executed (e.g. email sent)
    "meeting",       # autonomous/office: structured meeting summary (Enterprise)
}

MAX_PAYLOAD_CHARS = 8000


class InternalActionRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    agent_type: str = Field(min_length=1, max_length=64)
    action_type: str
    payload: dict = Field(default_factory=dict)
    session_id: Optional[str] = Field(default=None, max_length=256)
    channel: Optional[str] = Field(default=None, max_length=32)  # telegram | slack


def require_internal_token(x_internal_token: Optional[str] = Header(None)) -> None:
    """Shared-secret auth for the bridge gateway (same secret both directions)."""
    expected = os.getenv("TELEGRAM_GATEWAY_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="Internal token not configured")
    if not x_internal_token or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(status_code=403, detail="Forbidden")


def get_current_user_payload(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    payload = auth_service.verify_token(parts[1])
    if not payload or not payload.get("user_id"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return payload


@router.post("/internal", dependencies=[Depends(require_internal_token)])
def record_action(body: InternalActionRequest):
    """Store one structured action produced by an agent tool."""
    if body.action_type not in ACTION_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown action_type '{body.action_type}'")

    # Payload is LLM-authored — cap its serialized size instead of trusting it
    import json

    payload_str = json.dumps(body.payload, ensure_ascii=False)
    if len(payload_str) > MAX_PAYLOAD_CHARS:
        raise HTTPException(status_code=413, detail="Payload too large")

    now = datetime.utcnow()
    # Sortable id: timestamp prefix + random suffix (no user input in filename)
    action_id = f"{now.strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(4)}"
    record = {
        "action_id": action_id,
        "user_id": body.user_id,
        "agent_type": body.agent_type,
        "action_type": body.action_type,
        "payload": body.payload,
        "session_id": body.session_id,
        "channel": body.channel,
        "created_at": now.isoformat(),
    }
    db_service.save_json(ACTIONS_COLLECTION, action_id, record)
    logger.info(f"Agent action recorded: {body.action_type} by {body.agent_type} for {body.user_id}")
    return {"ok": True, "action_id": action_id}


@router.get("")
def list_actions(
    action_type: Optional[str] = None,
    agent_type: Optional[str] = None,
    limit: int = 50,
    user: dict = Depends(get_current_user_payload),
):
    """Newest-first action list for the dashboard."""
    limit = max(1, min(limit, 200))
    records = db_service.load_all_json(ACTIONS_COLLECTION) or []
    mine = [r for r in records if r.get("user_id") == user["user_id"]]
    if action_type:
        mine = [r for r in mine if r.get("action_type") == action_type]
    if agent_type:
        mine = [r for r in mine if r.get("agent_type") == agent_type]
    mine.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return {"actions": mine[:limit], "total": len(mine)}

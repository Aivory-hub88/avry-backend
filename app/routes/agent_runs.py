"""
"Is this agent doing something right now" — dashboard-facing read of the
live markers app/services/agent_run_tracker.py writes around every live
turn (Console chat, Telegram, Slack) in telegram_service.py's
_route_to_agent.

Dashboard-facing (JWT auth):
    GET /api/v1/agent-runs/active -> every agent_type currently in flight
                                      for this user
"""

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException

from app.database.db_service import DatabaseService
from app.services.auth_service import AuthService
from app.services.agent_run_tracker import list_running

router = APIRouter(prefix="/api/v1/agent-runs", tags=["agent-runs"])

db_service = DatabaseService()
auth_service = AuthService(db_service)


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


@router.get("/active")
async def list_active_runs(user: dict = Depends(get_current_user_payload)):
    return {"runs": list_running(user["user_id"])}

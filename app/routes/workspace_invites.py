"""
POST /api/v1/workspace-invites/notify  {workspace_id, user_id}

Called by the dashboard right after it adds a workspace member, so the person
gets an email. JWT-authenticated; the caller must be the one who added them.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.routes.agent_actions import get_current_user_payload
from app.services import workspace_invite

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/workspace-invites", tags=["workspace-invites"])


class NotifyBody(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)


@router.post("/notify")
async def notify(body: NotifyBody, user: dict = Depends(get_current_user_payload)):
    try:
        result = await workspace_invite.notify_member(
            body.workspace_id, body.user_id, str(user["user_id"])
        )
    except Exception as e:
        logger.warning("workspace invite notify failed: %s", e)
        return {"sent": False}
    if result == "not_member":
        raise HTTPException(status_code=404, detail="Not a member of that workspace")
    if result == "forbidden":
        raise HTTPException(status_code=403, detail="You did not add this member")
    return {"sent": result == "sent"}

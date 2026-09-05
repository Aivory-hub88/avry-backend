"""
Discord deployable-agent API endpoints.

Dashboard-facing (JWT auth):
    POST   /api/v1/discord/deploy-link        -> one-time connect code + bot invite URL
    GET    /api/v1/discord/link-status/{code} -> pending|connected|expired
    GET    /api/v1/discord/bindings           -> list connected channels
    DELETE /api/v1/discord/bindings/{id}      -> disconnect a channel

Listener-facing (X-Internal-Token, shared secret with vps-bridge/discord-listener.js):
    POST   /api/v1/discord/redeem   -> redeem a `/connect <code>` interaction
    POST   /api/v1/discord/message  -> route one channel message to the agent
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from app.database.db_service import DatabaseService
from app.routes.agent_actions import require_internal_token
from app.routes.telegram import get_current_user_payload
from app.services.discord_service import DiscordService, AGENT_TYPES

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/discord", tags=["discord"])

db_service = DatabaseService()
discord_service = DiscordService(db_service)


class DeployLinkRequest(BaseModel):
    agent_type: str


@router.post("/deploy-link")
def create_deploy_link(body: DeployLinkRequest, user: dict = Depends(get_current_user_payload)):
    """Generate a one-time connect code + the bot's invite URL."""
    try:
        return discord_service.create_link_token(user_id=user["user_id"], agent_type=body.agent_type)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except RuntimeError as e:
        logger.error(f"Discord not configured: {e}")
        raise HTTPException(status_code=503, detail="Discord integration is not configured")


@router.get("/link-status/{code}")
def link_status(code: str, user: dict = Depends(get_current_user_payload)):
    """Dashboard polls this after showing the connect code."""
    return discord_service.get_link_status(code, user["user_id"])


@router.get("/bindings")
def list_bindings(user: dict = Depends(get_current_user_payload)):
    return {"bindings": discord_service.list_bindings(user["user_id"])}


@router.delete("/bindings/{binding_id}")
def delete_binding(binding_id: str, user: dict = Depends(get_current_user_payload)):
    binding = discord_service.delete_binding(binding_id, user["user_id"])
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")
    # Best-effort farewell; the listener owns the actual Discord API call, so
    # this just marks the binding gone — nothing to send from here.
    return {"success": True}


@router.get("/agents")
def list_agent_types():
    return {"agents": [{"agent_type": k, "name": v} for k, v in AGENT_TYPES.items()]}


class RedeemRequest(BaseModel):
    code: str
    guild_id: str
    channel_id: str
    discord_user_id: str
    channel_name: Optional[str] = None


@router.post("/redeem", dependencies=[Depends(require_internal_token)])
def redeem(body: RedeemRequest):
    """Called by discord-listener.js when a user runs `/connect <code>`."""
    return discord_service.redeem_code(
        code=body.code,
        guild_id=body.guild_id,
        channel_id=body.channel_id,
        discord_user_id=body.discord_user_id,
        channel_name=body.channel_name,
    )


class MessageRequest(BaseModel):
    guild_id: str
    channel_id: str
    text: str
    attachments: Optional[list] = None


@router.post("/message", dependencies=[Depends(require_internal_token)])
def message(body: MessageRequest):
    """Called by discord-listener.js for every message in a channel it's
    watching. Returns {"reply": str | None} — None means the channel isn't
    bound to any agent and the listener should stay silent."""
    reply = discord_service.handle_message(
        guild_id=body.guild_id,
        channel_id=body.channel_id,
        text=body.text[:8000],
        attachments=body.attachments,
    )
    return {"reply": reply}

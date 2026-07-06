"""
Telegram deployable-agent API endpoints.

Dashboard-facing (JWT auth):
    POST   /api/v1/telegram/deploy-link       -> one-time QR deep link
    GET    /api/v1/telegram/link-status/{tok} -> pending|connected|expired
    GET    /api/v1/telegram/bindings          -> list connected chats
    DELETE /api/v1/telegram/bindings/{chat}   -> disconnect a chat

Telegram-facing:
    POST   /api/v1/telegram/webhook           -> Bot API updates
        (validated via X-Telegram-Bot-Api-Secret-Token)
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Header, Depends, Request
from pydantic import BaseModel

from app.config import settings
from app.database.db_service import DatabaseService
from app.services.auth_service import AuthService
from app.services.telegram_service import TelegramService, AGENT_TYPES

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/telegram", tags=["telegram"])

db_service = DatabaseService()
auth_service = AuthService(db_service)
telegram_service = TelegramService(db_service)


class DeployLinkRequest(BaseModel):
    agent_type: str
    chat_target: str = "private"  # or "group"


def get_current_user_payload(authorization: Optional[str] = Header(None)) -> dict:
    """Require a valid Bearer access token; return its JWT payload."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    payload = auth_service.verify_token(parts[1])
    if not payload or not payload.get("user_id"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return payload


@router.post("/deploy-link")
def create_deploy_link(
    body: DeployLinkRequest, user: dict = Depends(get_current_user_payload)
):
    """Generate a one-time deep link (rendered as QR by the dashboard)."""
    try:
        return telegram_service.create_link_token(
            user_id=user["user_id"],
            agent_type=body.agent_type,
            chat_target=body.chat_target,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        logger.error(f"Telegram not configured: {e}")
        raise HTTPException(status_code=503, detail="Telegram integration is not configured")


@router.get("/link-status/{token}")
def link_status(token: str, user: dict = Depends(get_current_user_payload)):
    """Dashboard polls this after showing the QR."""
    return telegram_service.get_link_status(token, user["user_id"])


@router.get("/bindings")
def list_bindings(user: dict = Depends(get_current_user_payload)):
    return {"bindings": telegram_service.list_bindings(user["user_id"])}


@router.delete("/bindings/{chat_id}")
def delete_binding(chat_id: int, user: dict = Depends(get_current_user_payload)):
    if not telegram_service.delete_binding(chat_id, user["user_id"]):
        raise HTTPException(status_code=404, detail="Binding not found")
    telegram_service.send_message(
        chat_id, "👋 Agent disconnected from your Aivory dashboard."
    )
    return {"success": True}


@router.get("/agents")
def list_agent_types():
    """Agent catalog the dashboard can deploy."""
    return {
        "agents": [{"agent_type": k, "name": v} for k, v in AGENT_TYPES.items()]
    }


@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None),
):
    """Receive Bot API updates. Always returns 200 so Telegram never retry-storms."""
    if not settings.telegram_webhook_secret or (
        x_telegram_bot_api_secret_token != settings.telegram_webhook_secret
    ):
        # Wrong/missing secret: reject so random POSTs can't inject updates
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    try:
        # process_update does blocking Bot API calls; keep it off the event loop
        import anyio

        await anyio.to_thread.run_sync(telegram_service.process_update, update)
    except Exception as e:
        # Never bubble errors back to Telegram — log and ack
        logger.error(f"Failed to process Telegram update: {e}", exc_info=True)

    return {"ok": True}

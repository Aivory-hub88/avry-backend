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
from app.services.telegram_service import (
    TelegramService,
    AGENT_TYPES,
    agent_tier_error,
    get_bot,
)

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
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except RuntimeError as e:
        logger.error(f"Telegram not configured: {e}")
        raise HTTPException(status_code=503, detail="Telegram integration is not configured")


class AgentChatRequest(BaseModel):
    agent_type: str
    text: str
    conversation_id: Optional[str] = None


@router.post("/agent-chat")
def agent_chat(body: AgentChatRequest, user: dict = Depends(get_current_user_payload)):
    """Talk to a deployable agent from the dashboard AI Console (JWT auth)."""
    if body.agent_type not in AGENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown agent_type '{body.agent_type}'")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    record = telegram_service._load_user(user["user_id"]) or {"user_id": user["user_id"]}
    tier_err = agent_tier_error(record, body.agent_type)
    if tier_err:
        raise HTTPException(status_code=403, detail=tier_err)

    reply = telegram_service.route_console_message(
        record, body.agent_type, text[:8000], body.conversation_id
    )
    return {"reply": reply, "agent_type": body.agent_type, "agent_name": AGENT_TYPES[body.agent_type]}


@router.get("/link-status/{token}")
def link_status(token: str, user: dict = Depends(get_current_user_payload)):
    """Dashboard polls this after showing the QR."""
    return telegram_service.get_link_status(token, user["user_id"])


@router.get("/bindings")
def list_bindings(user: dict = Depends(get_current_user_payload)):
    return {"bindings": telegram_service.list_bindings(user["user_id"])}


@router.delete("/bindings/{binding_id}")
def delete_binding(binding_id: str, user: dict = Depends(get_current_user_payload)):
    binding = telegram_service.delete_binding(binding_id, user["user_id"])
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")
    bot = get_bot(binding.get("agent_type"))
    if bot:
        telegram_service.send_message(
            bot, binding["chat_id"], "👋 Agent disconnected from your Aivory dashboard."
        )
    return {"success": True}


@router.get("/agents")
def list_agent_types():
    """Agent catalog the dashboard can deploy."""
    return {
        "agents": [{"agent_type": k, "name": v} for k, v in AGENT_TYPES.items()]
    }


async def _handle_webhook(request: Request, secret: Optional[str], bot: Optional[dict]):
    """Shared webhook handler. Always returns 200 so Telegram never retry-storms."""
    if not settings.telegram_webhook_secret or secret != settings.telegram_webhook_secret:
        # Wrong/missing secret: reject so random POSTs can't inject updates
        raise HTTPException(status_code=403, detail="Forbidden")
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not configured")

    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    try:
        # process_update does blocking Bot API calls; keep it off the event loop
        import anyio

        await anyio.to_thread.run_sync(telegram_service.process_update, update, bot)
    except Exception as e:
        # Never bubble errors back to Telegram — log and ack
        logger.error(f"Failed to process Telegram update: {e}", exc_info=True)

    return {"ok": True}


@router.post("/webhook/{bot_key}")
async def telegram_webhook_per_bot(
    bot_key: str,
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None),
):
    """Per-agent bot webhook (multi-bot mode)."""
    if bot_key not in AGENT_TYPES:
        raise HTTPException(status_code=404, detail="Unknown bot")
    return await _handle_webhook(
        request, x_telegram_bot_api_secret_token, get_bot(bot_key)
    )


@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None),
):
    """Legacy single-bot webhook (default bot)."""
    return await _handle_webhook(
        request, x_telegram_bot_api_secret_token, get_bot(None)
    )

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

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Header, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.database.db_service import DatabaseService
from app.services.auth_service import AuthService
from app.services import workspace_acting
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
    # ADR-020: the Team a Console Room turn belongs to (must be the caller's own).
    team_id: Optional[str] = Field(default=None, max_length=64)



# --- Long agent turns vs. the proxy in front of us -------------------------------------
# An agent turn can take well over two minutes (tool loops, slow model). The browser
# reaches this API through Cloudflare, which drops a request that has not started
# answering after ~120 s: the turn then finishes upstream but the user sees a vanished
# bubble (observed: agent-chat 499 after 125 s, Cloudflare client IP, while Cerveau
# completed the turn at 153 s). So a turn that is still running after FIRST_WAIT starts
# the response and sends a space every INTERVAL seconds (leading whitespace is valid
# JSON, so clients keep using res.json()). Quick requests, including validation errors
# with their proper HTTP status, behave exactly as before.
KEEPALIVE_FIRST_WAIT = 2.0
KEEPALIVE_INTERVAL = 15.0


async def _run_with_keepalive(fn, *, first_wait: float = KEEPALIVE_FIRST_WAIT, interval: float = KEEPALIVE_INTERVAL):
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(None, fn)
    try:
        return await asyncio.wait_for(asyncio.shield(fut), timeout=first_wait)
    except asyncio.TimeoutError:
        pass  # still running: stream a heartbeat until it finishes

    async def body():
        yield b" "  # start the response (headers) right away
        while True:
            try:
                result = await asyncio.wait_for(asyncio.shield(fut), timeout=interval)
                break
            except asyncio.TimeoutError:
                yield b" "
            except HTTPException as e:
                # Status is already 200 on the wire; carry the real one in the body.
                yield json.dumps({"detail": e.detail, "status_code": e.status_code}).encode()
                return
            except Exception:
                logger.exception("agent turn failed after the response had started")
                yield json.dumps({"detail": "Agent request failed", "status_code": 500}).encode()
                return
        yield json.dumps(result).encode()

    return StreamingResponse(body(), media_type="application/json")

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

    result = telegram_service.route_console_message(
        record, body.agent_type, text[:8000], body.conversation_id, team_id=body.team_id
    )
    return {
        "reply": result["reply"],
        "agent_type": body.agent_type,
        "agent_name": AGENT_TYPES[body.agent_type],
        "pending_approval": result.get("pending_approval"),
    }


@router.post("/agent-chat")
async def agent_chat_route(body: AgentChatRequest, user: dict = Depends(get_current_user_payload)):
    return await _run_with_keepalive(lambda: agent_chat(body, user))


class DiscussionTurnRequest(BaseModel):
    agent_type: str
    space_id: str
    thread_root: str
    text: str
    # ADR-020 §1.1: Workspace agents belong to the space's leader. When set
    # (and different from the caller), the turn runs in that user's tenant and
    # is billed to them; the caller (a member) is only the requester. Verified
    # server-side against the space's owner + the caller's write access.
    acting_as: Optional[str] = None


def discussion_turn(body: DiscussionTurnRequest, user: dict = Depends(get_current_user_payload)):
    """Talk to a deployable agent from a workspace Discussion room (JWT auth).

    Discussion is a deployment channel like console/telegram: identity comes
    from the discussion_* binding + shared room session, never from prompt
    coaching. Structured space/thread ids replace the opaque conversation_id.
    """
    if body.agent_type not in AGENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown agent_type '{body.agent_type}'")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    space_id = (body.space_id or "").strip()
    thread_root = (body.thread_root or "").strip()
    if not space_id or not thread_root:
        raise HTTPException(status_code=400, detail="space_id and thread_root are required")

    owner_id = user["user_id"]
    acting_as = (body.acting_as or "").strip()
    if acting_as and acting_as != owner_id:
        if not workspace_acting.can_act_as(space_id, owner_id, acting_as):
            raise HTTPException(status_code=403, detail="Not allowed to use this workspace's agents")
        if not telegram_service._load_user(acting_as):
            raise HTTPException(status_code=403, detail="Workspace leader account not found")
        logger.info(f"discussion-turn: {owner_id} acting as leader {acting_as} in space {space_id}")
        owner_id = acting_as

    record = telegram_service._load_user(owner_id) or {"user_id": owner_id}
    tier_err = agent_tier_error(record, body.agent_type)
    if tier_err:
        raise HTTPException(status_code=403, detail=tier_err)

    result = telegram_service.route_discussion_message(
        record,
        body.agent_type,
        space_id[:128],
        thread_root[:128],
        text[:8000],
        # Only the tenant owner's own session may answer a pending approval by text.
        allow_approval_text=(owner_id == user["user_id"]),
    )
    return {
        "reply": result["reply"],
        "agent_type": body.agent_type,
        "agent_name": AGENT_TYPES[body.agent_type],
        "pending_approval": result.get("pending_approval"),
    }


@router.post("/discussion-turn")
async def discussion_turn_route(body: DiscussionTurnRequest, user: dict = Depends(get_current_user_payload)):
    return await _run_with_keepalive(lambda: discussion_turn(body, user))


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

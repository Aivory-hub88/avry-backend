"""
WhatsApp deployable-agent API endpoints (Kapso transport, shared Aivory number).

Dashboard-facing (JWT auth):
    POST   /api/v1/whatsapp/deploy-link        -> one-time LINK code + wa.me deep link (QR)
    GET    /api/v1/whatsapp/link-status/{tok}  -> pending|connected|expired|not_found
    GET    /api/v1/whatsapp/bindings           -> list connected numbers
    DELETE /api/v1/whatsapp/bindings/{id}      -> disconnect a number

Dashboard server-to-server (X-Agent-Turn-Token, same secret as /telegram/internal/agent-turn):
    POST   /api/v1/whatsapp/inbound            -> a Kapso message forwarded by the dashboard webhook
    PUT    /api/v1/whatsapp/internal/addon     -> switch the WhatsApp add-on on/off for a tenant
"""

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from app.routes.telegram import (
    db_service,
    get_current_user_payload,
    require_agent_turn_token,
    _require_paid_agents,
)
from app.services.whatsapp_service import AddonRequired, WhatsAppService, normalize_wa_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/whatsapp", tags=["whatsapp"])

whatsapp_service = WhatsAppService(db_service)

ADDON_REQUIRED_CODE = "whatsapp_addon_required"


class DeployLinkRequest(BaseModel):
    agent_type: str


@router.post("/deploy-link")
def create_deploy_link(body: DeployLinkRequest, user: dict = Depends(get_current_user_payload)):
    """One-time LINK code + wa.me deep link (the dashboard renders it as a QR)."""
    _require_paid_agents(user["user_id"])
    try:
        return whatsapp_service.create_link_token(user["user_id"], body.agent_type)
    except AddonRequired as e:
        raise HTTPException(status_code=402, detail={"code": ADDON_REQUIRED_CODE, "message": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except RuntimeError as e:
        logger.error("WhatsApp not configured: %s", e)
        raise HTTPException(status_code=503, detail="WhatsApp integration is not configured")


@router.get("/link-status/{token}")
def link_status(token: str, user: dict = Depends(get_current_user_payload)):
    return whatsapp_service.get_link_status(token, user["user_id"])


@router.get("/bindings")
def list_bindings(user: dict = Depends(get_current_user_payload)):
    out = []
    for b in whatsapp_service.list_bindings(user["user_id"]):
        wa = b.get("wa_id") or ""
        out.append({
            "binding_id": b.get("binding_id"),
            "agent_type": b.get("agent_type"),
            "agent_name": b.get("agent_name"),
            "wa_name": b.get("wa_name"),
            # The number is the user's own, but keep it masked in list payloads.
            "masked_number": f"+{wa[:2]}{'•' * max(len(wa) - 6, 0)}{wa[-4:]}" if len(wa) >= 7 else None,
            "created_at": b.get("created_at"),
        })
    return {"bindings": out}


@router.delete("/bindings/{binding_id}")
def delete_binding(binding_id: str, user: dict = Depends(get_current_user_payload)):
    binding = whatsapp_service.delete_binding(binding_id, user["user_id"])
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")
    whatsapp_service.send_text(binding["wa_id"], "👋 Agent diputus dari dashboard Aivory kamu.")
    return {"success": True}


class InboundMessage(BaseModel):
    message_id: str = Field(min_length=1, max_length=200)
    # Sender number, digits (the dashboard strips "+" and spaces).
    from_: str = Field(alias="from", min_length=7, max_length=32)
    name: Optional[str] = Field(default=None, max_length=200)
    type: str = Field(default="text", max_length=32)
    text: str = Field(default="", max_length=12000)
    media_id: Optional[str] = Field(default=None, max_length=128)
    mime_type: Optional[str] = Field(default=None, max_length=128)
    filename: Optional[str] = Field(default=None, max_length=255)
    duration: Optional[int] = Field(default=None, ge=0, le=86400)

    model_config = {"populate_by_name": True}


@router.post("/inbound", dependencies=[Depends(require_agent_turn_token)])
def inbound(body: InboundMessage, background: BackgroundTasks):
    """
    Decide who owns a message the dashboard received from Kapso.

    - `not_handled`: the number is not bound to an agent. The dashboard keeps its CRM flow.
    - `duplicate`:   already processed (provider retry). The dashboard should do nothing.
    - `handled`:     a link code was redeemed, or a bound number's message is running through
                     its agent. The agent turn can take minutes, so it runs after this returns.
    """
    msg = body.model_dump(by_alias=True)
    decision = whatsapp_service.classify_inbound(msg)
    if decision["reply"]:
        whatsapp_service.send_text(normalize_wa_id(msg["from"]), decision["reply"])
    if decision["binding"]:
        background.add_task(whatsapp_service.run_turn, decision["binding"], msg)
    return {"status": decision["status"]}


class AddonRequest(BaseModel):
    tenant_user_id: str = Field(min_length=1, max_length=128)
    active: bool
    expires_at: Optional[str] = Field(default=None, max_length=40)  # ISO-8601 UTC, or null for no expiry


@router.put("/internal/addon", dependencies=[Depends(require_agent_turn_token)])
def set_addon(body: AddonRequest):
    """Called by the dashboard's billing flow when the add-on is bought, renewed or lapses."""
    return whatsapp_service.set_addon(body.tenant_user_id, body.active, body.expires_at)

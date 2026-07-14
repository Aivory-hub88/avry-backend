"""
Credit API — LLM-usage metering for deployable agents.

Internal (bridge-facing, X-Internal-Token == TELEGRAM_GATEWAY_TOKEN):
    POST /api/v1/credits/internal/consume       -> deduct credits (402 when short)
    GET  /api/v1/credits/internal/status/{uid}  -> balance snapshot

Dashboard-facing (JWT auth):
    GET  /api/v1/credits                        -> caller's balance/allowance
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.routes.agent_actions import get_current_user_payload, require_internal_token
from app.services import credit_service
from app.services.credit_service import InsufficientCredits

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/credits", tags=["credits"])

VALID_REASONS = {"agent_message", "diagnostic", "blueprint", "roadmap", "console"}


class ConsumeRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    amount: int = Field(default=1, ge=1, le=100)
    reason: str = Field(default="agent_message", max_length=64)
    meta: Optional[dict] = None


@router.post("/internal/consume", dependencies=[Depends(require_internal_token)])
def consume_credits(body: ConsumeRequest):
    if body.reason not in VALID_REASONS:
        raise HTTPException(status_code=400, detail=f"Unknown reason '{body.reason}'")
    try:
        result = credit_service.consume(body.user_id, body.amount, body.reason, body.meta)
    except InsufficientCredits as e:
        raise HTTPException(
            status_code=402,
            detail={"error": "insufficient_credits", "balance": e.balance},
        )
    except Exception as e:
        logger.error(f"credit consume failed for {body.user_id}: {e}")
        raise HTTPException(status_code=503, detail="Credit service unavailable")
    return {"ok": True, **result}


@router.get("/internal/status/{user_id}", dependencies=[Depends(require_internal_token)])
def internal_status(user_id: str):
    try:
        return credit_service.get_status(user_id)
    except Exception as e:
        logger.error(f"credit status failed for {user_id}: {e}")
        raise HTTPException(status_code=503, detail="Credit service unavailable")


@router.get("")
def my_credits(user: dict = Depends(get_current_user_payload)):
    try:
        return credit_service.get_status(user["user_id"])
    except Exception as e:
        logger.error(f"credit status failed for {user['user_id']}: {e}")
        raise HTTPException(status_code=503, detail="Credit service unavailable")

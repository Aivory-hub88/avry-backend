"""Passkey endpoints, mounted under /api/v1/auth/passkey."""
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.database import pg_service as pg
from app.models.user import AuthResponse
from app.routes.auth import auth_service, get_token_from_header
from app.services import passkey_service as passkeys
from app.services.auth_service import PaymentRequiredError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth/passkey", tags=["passkeys"])


class RegisterVerify(BaseModel):
    challenge_token: str
    credential: Dict[str, Any]
    name: Optional[str] = None


class LoginVerify(BaseModel):
    challenge_token: str
    credential: Dict[str, Any]


async def _current_user(authorization: Optional[str]) -> dict:
    token = get_token_from_header(authorization)
    payload = auth_service.verify_token(token) if token else None
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not await pg.is_available():
        raise HTTPException(status_code=503, detail="Passkeys are unavailable right now")
    user = await pg.get_user_by_id(payload.get("user_id"))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = dict(user)
    user.setdefault("user_id", user.get("id"))
    return user


@router.post("/register/options")
async def register_options(authorization: Optional[str] = Header(None)):
    user = await _current_user(authorization)
    try:
        return await passkeys.registration_options(user)
    except passkeys.PasskeyError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/register/verify")
async def register_verify(body: RegisterVerify, authorization: Optional[str] = Header(None)):
    user = await _current_user(authorization)
    try:
        await passkeys.verify_registration(user, body.challenge_token, body.credential, body.name or "")
    except passkeys.PasskeyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@router.post("/login/options")
async def login_options():
    if not await pg.is_available():
        raise HTTPException(status_code=503, detail="Passkeys are unavailable right now")
    return passkeys.login_options()


@router.post("/login/verify", response_model=AuthResponse)
async def login_verify(body: LoginVerify):
    if not await pg.is_available():
        raise HTTPException(status_code=503, detail="Passkeys are unavailable right now")
    try:
        user = await passkeys.verify_login(body.challenge_token, body.credential)
        return await auth_service.complete_login(user, pg_up=True)
    except PaymentRequiredError as e:
        raise HTTPException(
            status_code=402,
            detail={"error": "payment_required", "deadline_at": e.deadline_at.isoformat()},
        )
    except passkeys.PasskeyError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.get("")
async def list_passkeys(authorization: Optional[str] = Header(None)):
    user = await _current_user(authorization)
    rows = await pg.list_passkeys(user["user_id"])
    return {
        "passkeys": [
            {
                "id": r["id"],
                "name": r["name"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
            }
            for r in rows
        ]
    }


@router.delete("/{passkey_id}")
async def delete_passkey(passkey_id: int, authorization: Optional[str] = Header(None)):
    user = await _current_user(authorization)
    if not await pg.delete_passkey(user["user_id"], passkey_id):
        raise HTTPException(status_code=404, detail="Passkey not found")
    return {"ok": True}

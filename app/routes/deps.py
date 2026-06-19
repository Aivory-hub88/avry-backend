"""Shared FastAPI auth dependencies — JWT bearer validation."""
import os
from typing import Optional

import jwt
from fastapi import Header, HTTPException, Depends

JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"


def current_payload(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def require_admin(payload: dict = Depends(current_payload)) -> dict:
    if payload.get("account_type") not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return payload


def require_superadmin(payload: dict = Depends(current_payload)) -> dict:
    if payload.get("account_type") != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")
    return payload

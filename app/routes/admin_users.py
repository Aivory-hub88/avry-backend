"""
Admin Users API endpoints — manages users and admin accounts.

Provides endpoints for the admin dashboard to list all users,
list admin accounts, create/suspend/reactivate admin accounts.
"""

import os
import logging
from typing import Optional

import jwt
import bcrypt
from fastapi import APIRouter, HTTPException, Header, Query

from app.database import pg_service as pg
from app.utils.id_generator import generate_id

logger = logging.getLogger(__name__)

JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"

router = APIRouter(prefix="/api/v1/admin", tags=["admin-users"])


# ── Auth Helpers ──────────────────────────────────────────────────────────────


async def require_admin(authorization: Optional[str] = Header(None)) -> dict:
    """
    Validate admin or superadmin access from Bearer token.
    Returns the decoded JWT payload.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="No authorization token provided")

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    token = parts[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    account_type = payload.get("account_type")
    if account_type not in ("superadmin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")

    return payload


# ── GET /api/v1/admin/users — List all users ─────────────────────────────────


@router.get("/users")
async def list_users(authorization: Optional[str] = Header(None)):
    """List all users with their account type, tier info, and credits."""
    await require_admin(authorization)

    pool = await pg.get_pool()
    rows = await pool.fetch(
        """
        SELECT id, email, account_type, company_name, is_active,
               created_at, updated_at
        FROM users
        ORDER BY created_at DESC
        """
    )

    users = []
    for row in rows:
        users.append({
            "userId": row["id"],
            "email": row["email"],
            "accountType": row["account_type"],
            "companyName": row["company_name"],
            "isActive": row["is_active"],
            "tier": row["account_type"],  # simplified — tier = account_type for now
            "creditsUsed": 0,
            "creditsMax": _get_credits_max(row["account_type"]),
            "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
            "payments": [],
        })

    return {"users": users}


# ── GET /api/v1/admin/admin-accounts — List admin/superadmin accounts ────────


@router.get("/admin-accounts")
async def list_admin_accounts(authorization: Optional[str] = Header(None)):
    """List all admin and superadmin accounts."""
    await require_admin(authorization)

    pool = await pg.get_pool()
    rows = await pool.fetch(
        """
        SELECT id, email, account_type, is_active, created_at, updated_at
        FROM users
        WHERE account_type IN ('admin', 'superadmin')
        ORDER BY created_at DESC
        """
    )

    admins = []
    for row in rows:
        is_active = row["is_active"]
        email = row["email"]
        admins.append({
            # camelCase (for Settings page)
            "id": row["id"],
            "email": email,
            "fullName": email.split("@")[0],
            "accountType": row["account_type"],
            "isActive": is_active,
            "status": "active" if is_active else "suspended",
            "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
            "updatedAt": row["updated_at"].isoformat() if row["updated_at"] else None,
            # snake_case (for AdminTable component)
            "full_name": email.split("@")[0],
            "account_type": row["account_type"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "created_by": "system",
            "banned_at": None if is_active else (row["updated_at"].isoformat() if row["updated_at"] else None),
            "ban_duration": None if is_active else "indefinite",
            "email_confirmed_at": row["created_at"].isoformat() if row["created_at"] else None,
        })

    return {"admins": admins, "total": len(admins)}


# ── POST /api/v1/admin/admin-accounts — Create admin account ─────────────────


@router.post("/admin-accounts")
async def create_admin_account(
    body: dict,
    authorization: Optional[str] = Header(None),
):
    """Create a new admin or superadmin account. Superadmin only."""
    payload = await require_admin(authorization)

    # Only superadmins can create other admins
    if payload.get("account_type") != "superadmin":
        raise HTTPException(status_code=403, detail="Only superadmins can create admin accounts")

    email = body.get("email")
    password = body.get("password")
    account_type = body.get("accountType", "admin")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")

    if account_type not in ("admin", "superadmin"):
        raise HTTPException(status_code=400, detail="accountType must be 'admin' or 'superadmin'")

    pool = await pg.get_pool()

    # Check if email already exists
    existing = await pool.fetchrow("SELECT id FROM users WHERE email = $1", email)
    if existing:
        raise HTTPException(status_code=409, detail="Email already exists")

    # Hash password
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()

    # Create user
    user_id = generate_id("user")
    await pool.execute(
        """
        INSERT INTO users (id, email, password_hash, account_type, is_active)
        VALUES ($1, $2, $3, $4, true)
        """,
        user_id, email, password_hash, account_type,
    )

    return {
        "success": True,
        "admin": {
            "id": user_id,
            "email": email,
            "accountType": account_type,
            "isActive": True,
        },
    }


# ── PATCH /api/v1/admin/admin-accounts/{id}/suspend ──────────────────────────


@router.patch("/admin-accounts/{user_id}/suspend")
async def suspend_admin(user_id: str, authorization: Optional[str] = Header(None)):
    """Suspend an admin account (set is_active = false). Superadmin only."""
    payload = await require_admin(authorization)

    if payload.get("account_type") != "superadmin":
        raise HTTPException(status_code=403, detail="Only superadmins can suspend admins")

    # Prevent self-suspension
    if payload.get("user_id") == user_id:
        raise HTTPException(status_code=400, detail="Cannot suspend your own account")

    pool = await pg.get_pool()
    result = await pool.execute(
        "UPDATE users SET is_active = false, updated_at = NOW() WHERE id = $1",
        user_id,
    )

    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="User not found")

    return {"success": True, "message": "Account suspended"}


# ── PATCH /api/v1/admin/admin-accounts/{id}/reactivate ───────────────────────


@router.patch("/admin-accounts/{user_id}/reactivate")
async def reactivate_admin(user_id: str, authorization: Optional[str] = Header(None)):
    """Reactivate a suspended admin account. Superadmin only."""
    payload = await require_admin(authorization)

    if payload.get("account_type") != "superadmin":
        raise HTTPException(status_code=403, detail="Only superadmins can reactivate admins")

    pool = await pg.get_pool()
    result = await pool.execute(
        "UPDATE users SET is_active = true, updated_at = NOW() WHERE id = $1",
        user_id,
    )

    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="User not found")

    return {"success": True, "message": "Account reactivated"}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_credits_max(account_type: str) -> int:
    """Return max credits based on account type."""
    return {
        "free": 10,
        "snapshot": 50,
        "blueprint": 100,
        "enterprise": 2000,
        "superadmin": 2000,
        "admin": 500,
    }.get(account_type, 10)

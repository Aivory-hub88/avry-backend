"""
Admin Users API endpoints — manages users and admin accounts.

Provides endpoints for the admin dashboard to list all users,
list admin accounts, create/suspend/reactivate admin accounts.
"""

import os
import secrets
import string
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

# Account types that the admin dashboard's "admin accounts" screen manages.
# `demo` accounts are limited product users (created for demos/tests) — they are
# created and listed alongside admins so superadmins manage them in one place,
# but the product itself restricts a demo login to a fixed set of modules.
MANAGED_ACCOUNT_TYPES = ("admin", "superadmin", "demo")

# Sidebar/module keys a demo account may be granted access to. Mirrors
# DEMO_ALLOWED_NAV_KEYS in avry-user-dashboard's lib/moduleAccess.ts — keep in
# sync when adding a module there.
VALID_MODULE_KEYS = (
    "console", "diagnostics", "blueprint", "roadmap", "workflows",
    "executionLogs", "integrations", "templates", "agents", "profile",
)

# Applied when a demo account is created without an explicit `allowedModules`
# list (e.g. older admin dashboard builds) — matches the previous hardcoded
# fixed set so existing behavior doesn't change.
DEFAULT_DEMO_MODULES = ["console", "diagnostics", "blueprint", "roadmap"]

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
        SELECT id, email, account_type, is_active, allowed_modules, created_at, updated_at
        FROM users
        WHERE account_type IN ('admin', 'superadmin', 'demo')
        ORDER BY created_at DESC
        """
    )

    admins = []
    for row in rows:
        is_active = row["is_active"]
        email = row["email"]
        allowed_modules = row["allowed_modules"]
        admins.append({
            # camelCase (for Settings page)
            "id": row["id"],
            "email": email,
            "fullName": email.split("@")[0],
            "accountType": row["account_type"],
            "isActive": is_active,
            "status": "active" if is_active else "suspended",
            "allowedModules": allowed_modules,
            "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
            "updatedAt": row["updated_at"].isoformat() if row["updated_at"] else None,
            # snake_case (for AdminTable component)
            "full_name": email.split("@")[0],
            "account_type": row["account_type"],
            "allowed_modules": allowed_modules,
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
    """
    Create a new managed account.

    - `admin` / `superadmin` accounts: superadmin only.
    - `demo` accounts (limited product users for demos/tests): any admin or
      superadmin. The frontend passes `accountType: "demo"`.
    """
    payload = await require_admin(authorization)

    email = body.get("email")
    password = body.get("password")
    account_type = body.get("accountType", "admin")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required")

    if account_type not in MANAGED_ACCOUNT_TYPES:
        raise HTTPException(
            status_code=400,
            detail="accountType must be 'admin', 'superadmin', or 'demo'",
        )

    # Only superadmins can mint other privileged (admin/superadmin) accounts.
    # Demo accounts are low-privilege and may be created by any admin.
    if account_type in ("admin", "superadmin") and payload.get("account_type") != "superadmin":
        raise HTTPException(status_code=403, detail="Only superadmins can create admin accounts")

    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters long")

    # Demo accounts get a per-account module allowlist (which sidebar entries
    # / routes they may use in the user dashboard). Non-demo accounts ignore
    # this — admins/superadmins already have full access.
    allowed_modules = None
    if account_type == "demo":
        raw_modules = body.get("allowedModules")
        if raw_modules is None:
            allowed_modules = list(DEFAULT_DEMO_MODULES)
        else:
            if not isinstance(raw_modules, list) or not all(isinstance(m, str) for m in raw_modules):
                raise HTTPException(status_code=400, detail="allowedModules must be a list of strings")
            invalid = [m for m in raw_modules if m not in VALID_MODULE_KEYS]
            if invalid:
                raise HTTPException(status_code=400, detail=f"Unknown module(s): {', '.join(invalid)}")
            # 'console' is the demo home route — always included so a demo
            # login always has somewhere to land.
            allowed_modules = sorted(set(raw_modules) | {"console"})

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
        INSERT INTO users (id, email, password_hash, account_type, is_active, allowed_modules)
        VALUES ($1, $2, $3, $4, true, $5)
        """,
        user_id, email, password_hash, account_type, allowed_modules,
    )

    return {
        "success": True,
        "admin": {
            "id": user_id,
            "email": email,
            "accountType": account_type,
            "isActive": True,
            "allowedModules": allowed_modules,
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


# ── POST /api/v1/admin/admin-accounts/{id}/reset-password ────────────────────


@router.post("/admin-accounts/{user_id}/reset-password")
async def reset_account_password(
    user_id: str,
    body: Optional[dict] = None,
    authorization: Optional[str] = Header(None),
):
    """
    Set a new password for a managed account.

    If the request body contains a `password`, it is used (this backs the admin
    dashboard's "Change Password" action for demo accounts). Otherwise a strong
    random password is generated and returned so the admin can share it.

    Privilege: resetting an admin/superadmin password requires superadmin;
    resetting a demo account's password may be done by any admin.
    """
    payload = await require_admin(authorization)

    pool = await pg.get_pool()
    target = await pool.fetchrow(
        "SELECT id, account_type FROM users WHERE id = $1", user_id
    )
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target["account_type"] in ("admin", "superadmin") and payload.get("account_type") != "superadmin":
        raise HTTPException(status_code=403, detail="Only superadmins can reset admin passwords")

    body = body or {}
    new_password = body.get("password")
    generated = False
    if new_password:
        if len(new_password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters long")
    else:
        new_password = _generate_password()
        generated = True

    password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(12)).decode()
    await pool.execute(
        "UPDATE users SET password_hash = $1, updated_at = NOW() WHERE id = $2",
        password_hash, user_id,
    )

    result = {"success": True, "message": "Password updated"}
    # Only echo the password back when WE generated it — never reflect a
    # caller-supplied secret in the response body/logs.
    if generated:
        result["password"] = new_password
    return result


# ── PATCH /api/v1/admin/admin-accounts/{id}/modules ───────────────────────────


@router.patch("/admin-accounts/{user_id}/modules")
async def update_account_modules(
    user_id: str,
    body: dict,
    authorization: Optional[str] = Header(None),
):
    """
    Update a demo account's module allowlist. Any admin or superadmin may
    call this (same privilege level as creating a demo account) — it only
    ever touches `demo` accounts, never admin/superadmin ones.
    """
    await require_admin(authorization)

    pool = await pg.get_pool()
    target = await pool.fetchrow(
        "SELECT id, account_type FROM users WHERE id = $1", user_id
    )
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target["account_type"] != "demo":
        raise HTTPException(
            status_code=400,
            detail="Module access can only be edited for demo accounts",
        )

    raw_modules = body.get("allowedModules")
    if not isinstance(raw_modules, list) or not all(isinstance(m, str) for m in raw_modules):
        raise HTTPException(status_code=400, detail="allowedModules must be a list of strings")
    invalid = [m for m in raw_modules if m not in VALID_MODULE_KEYS]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown module(s): {', '.join(invalid)}")

    # 'console' is the demo home route — always included.
    allowed_modules = sorted(set(raw_modules) | {"console"})

    await pool.execute(
        "UPDATE users SET allowed_modules = $1, updated_at = NOW() WHERE id = $2",
        allowed_modules, user_id,
    )

    return {"success": True, "allowedModules": allowed_modules}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _generate_password(length: int = 16) -> str:
    """Generate a strong random password (letters, digits, symbols)."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()_+-="
    return "".join(secrets.choice(alphabet) for _ in range(length))


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

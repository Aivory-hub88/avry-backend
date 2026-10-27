"""
Admin Users API endpoints — manages users and admin accounts.

Provides endpoints for the admin dashboard to list all users,
list admin accounts, create/suspend/reactivate admin accounts.
"""

import json
import secrets
import string
import logging
from typing import Optional

import jwt
import bcrypt
from fastapi import APIRouter, HTTPException, Header, Query

from app.database import pg_service as pg
from app.services import demo_data_cleanup, password_reset_service, tiers
from app.utils.id_generator import generate_id

logger = logging.getLogger(__name__)

from app.services.jwt_secret import JWT_SECRET
JWT_ALGORITHM = "HS256"

# Account types that the admin dashboard's "admin accounts" screen manages.
# `demo` accounts are limited product users (created for demos/tests) — they are
# created and listed alongside admins so superadmins manage them in one place,
# but the product itself restricts a demo login to a fixed set of modules.
# `partner` accounts are for distributor partners: every user-dashboard feature
# at the Enterprise tier, but never Aivory's own self-hosted n8n instance —
# that stays superadmin-only (avry-user-dashboard `canUseAivoryN8nInstance`).
MANAGED_ACCOUNT_TYPES = ("admin", "superadmin", "demo", "partner")

# What a partner account is granted. It is written as a regular `user_tiers`
# row rather than special-cased per gate, so every tier check that already
# understands Enterprise — credits, agents, MCP servers, schedules — treats a
# partner exactly like an Enterprise customer without each one learning a new
# account type. The access is free for a fixed number of months; after
# `expires_at` the partner keeps going on a paid subscription (and buys
# Intelligence Credits) like any customer, or a superadmin extends the free
# period. Partners are exempt from the lapsed-account cleanup, so a lapse
# never deletes the account.
PARTNER_TIER = "enterprise"
PARTNER_FEATURES = ["snapshot", "blueprint"]
PARTNER_FREE_MONTH_OPTIONS = (1, 3, 5)

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
        SELECT u.id, u.email, u.account_type, u.company_name, u.is_active,
               u.created_at, u.updated_at,
               CASE WHEN t.expires_at IS NULL OR t.expires_at > now() THEN t.tier END AS live_tier,
               t.expires_at, t.features
        FROM users u
        LEFT JOIN user_tiers t ON t.user_id = u.id
        ORDER BY u.created_at DESC
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
            # The plan the account holds right now, from user_tiers (where
            # payments land). `free` means no live plan — the account is not
            # activated; there is no free product. It used to echo
            # account_type, which showed every customer as "free"/"paid".
            "tier": _live_plan(row["account_type"], row["live_tier"], row["features"]),
            "tierExpiresAt": row["expires_at"].isoformat() if row["expires_at"] else None,
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
        SELECT u.id, u.email, u.account_type, u.company_name, u.is_active,
               u.allowed_modules, u.created_at, u.updated_at,
               CASE WHEN u.account_type = 'partner' THEN t.expires_at END AS partner_expires_at
        FROM users u
        LEFT JOIN user_tiers t ON t.user_id = u.id
        WHERE u.account_type IN ('admin', 'superadmin', 'demo', 'partner')
        ORDER BY u.created_at DESC
        """
    )

    admins = []
    for row in rows:
        is_active = row["is_active"]
        email = row["email"]
        partner_expires_at = (
            row["partner_expires_at"].isoformat() if row["partner_expires_at"] else None
        )
        allowed_modules = row["allowed_modules"]
        admins.append({
            # camelCase (for Settings page)
            "id": row["id"],
            "email": email,
            "fullName": email.split("@")[0],
            "accountType": row["account_type"],
            "companyName": row["company_name"],
            "partnerExpiresAt": partner_expires_at,
            "isActive": is_active,
            "status": "active" if is_active else "suspended",
            "allowedModules": allowed_modules,
            "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
            "updatedAt": row["updated_at"].isoformat() if row["updated_at"] else None,
            # snake_case (for AdminTable component)
            "full_name": email.split("@")[0],
            "account_type": row["account_type"],
            "company_name": row["company_name"],
            "partner_expires_at": partner_expires_at,
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
    - `partner` accounts (distributor partners): superadmin only, since they
      carry the Enterprise feature set without paying. `companyName` (the
      distributor) and `freeMonths` (1, 3 or 5) are required.
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
            detail="accountType must be 'admin', 'superadmin', 'demo', or 'partner'",
        )

    # Only superadmins can mint other privileged (admin/superadmin) accounts,
    # or partner accounts, which hold a paid tier for free. Demo accounts are
    # low-privilege and may be created by any admin.
    if account_type in ("admin", "superadmin", "partner") and payload.get("account_type") != "superadmin":
        raise HTTPException(status_code=403, detail=f"Only superadmins can create {account_type} accounts")

    company_name = (body.get("companyName") or "").strip() or None
    if account_type == "partner" and not company_name:
        raise HTTPException(status_code=400, detail="companyName is required for partner accounts")
    free_months = _partner_free_months(body) if account_type == "partner" else None

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

    # Create user (and, for a partner, its Enterprise entitlement) in one
    # transaction, so a partner can never exist without its access.
    user_id = generate_id("user")
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO users (id, email, password_hash, account_type, is_active,
                                   allowed_modules, company_name)
                VALUES ($1, $2, $3, $4, true, $5, $6)
                """,
                user_id, email, password_hash, account_type, allowed_modules, company_name,
            )
            if account_type == "partner":
                await conn.execute(
                    """
                    INSERT INTO user_tiers (user_id, tier, features, expires_at, updated_at)
                    VALUES ($1, $2, $3::jsonb, now() + make_interval(months => $4), now())
                    ON CONFLICT (user_id) DO UPDATE
                       SET tier = EXCLUDED.tier, features = EXCLUDED.features,
                           expires_at = EXCLUDED.expires_at, updated_at = now()
                    """,
                    user_id, PARTNER_TIER, json.dumps(PARTNER_FEATURES), free_months,
                )

    return {
        "success": True,
        "admin": {
            "id": user_id,
            "email": email,
            "accountType": account_type,
            "companyName": company_name,
            "freeMonths": free_months,
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


# ── POST /api/v1/admin/admin-accounts/{id}/logout ─────────────────────────────


@router.post("/admin-accounts/{user_id}/logout")
async def logout_demo_account(user_id: str, authorization: Optional[str] = Header(None)):
    """
    Force-logout a demo account and wipe its usage data (Deep Diagnostic,
    Blueprint, Roadmap, Workflow Copilot content). Demo accounts get handed
    to different prospects over time; without this, the next demo call would
    otherwise see whatever the previous one produced (see demo_data_cleanup.py).

    Demo accounts only — this is a data-destructive action and every other
    account type has a real owner who should not have their content wiped by
    an admin click.
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
            detail="Logout-and-cleanup is only available for demo accounts",
        )

    sessions_ended = await demo_data_cleanup.invalidate_sessions(user_id)
    cleared = await demo_data_cleanup.clear_usage_data(user_id)

    return {
        "success": True,
        "message": "Demo account logged out and usage data cleared",
        "sessionsEnded": sessions_ended,
        "cleared": cleared,
    }


# ── POST /api/v1/admin/users/{id}/reset-password ──────────────────────────────


@router.post("/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: str,
    body: Optional[dict] = None,
    authorization: Optional[str] = Header(None),
):
    """
    Reset any user's password — the general counterpart of
    /admin-accounts/{id}/reset-password above, which only ever covered
    admin/superadmin/demo accounts. A locked-out regular customer had no path
    at all before this.

    mode:
      - "email" (default): mail a one-time reset link, change nothing yet.
      - "set": apply the supplied password immediately.
      - "generate": mint a strong password server-side and return it once.

    "set"/"generate" both end every active session for the account — the
    frontend tells the admin this happens, so it must actually happen.
    Privilege: resetting an admin/superadmin password requires superadmin.
    `clearUsageData: true` additionally wipes the account's Deep
    Diagnostic/Blueprint/Roadmap/Workflow content — demo accounts only.
    """
    payload = await require_admin(authorization)

    pool = await pg.get_pool()
    target = await pool.fetchrow(
        "SELECT id, email, account_type, is_active FROM users WHERE id = $1", user_id
    )
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target["account_type"] in ("admin", "superadmin") and payload.get("account_type") != "superadmin":
        raise HTTPException(status_code=403, detail="Only superadmins can reset admin passwords")

    body = body or {}
    mode = body.get("mode") or ("set" if body.get("password") else "email")
    if mode not in ("email", "set", "generate"):
        raise HTTPException(status_code=400, detail="mode must be one of: email, set, generate")

    clear_usage_data = bool(body.get("clearUsageData"))
    if clear_usage_data and target["account_type"] != "demo":
        raise HTTPException(
            status_code=400,
            detail="clearUsageData is only available for demo accounts",
        )

    if mode == "email":
        sent = await password_reset_service.request_reset(target["email"])
        # clearUsageData is independent of *when* the password actually
        # changes (the user hasn't clicked the link yet) — an admin clearing
        # a demo account's content wants that to happen now, not on click.
        if clear_usage_data:
            await demo_data_cleanup.clear_usage_data(user_id)
        return {
            "success": True,
            "emailSent": sent,
            "message": "Reset link sent" if sent else "Could not send the reset email",
        }

    new_password = body.get("password") if mode == "set" else None
    generated_password = None
    if mode == "set":
        if not new_password or len(new_password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters long")
    else:  # generate
        new_password = _generate_password()
        generated_password = new_password

    password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(12)).decode()
    await pool.execute(
        "UPDATE users SET password_hash = $1, updated_at = NOW() WHERE id = $2",
        password_hash, user_id,
    )
    # An older emailed reset link must not be able to undo this, and every
    # session logged in under the old password must end.
    await password_reset_service.invalidate_all(user_id)
    await demo_data_cleanup.invalidate_sessions(user_id)

    if clear_usage_data:
        await demo_data_cleanup.clear_usage_data(user_id)

    result = {"success": True, "message": "Password updated. Signed out of all sessions."}
    if generated_password:
        result["password"] = generated_password
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


# ── PATCH /api/v1/admin/admin-accounts/{id}/partner-access ───────────────────


@router.patch("/admin-accounts/{user_id}/partner-access")
async def extend_partner_access(
    user_id: str,
    body: dict,
    authorization: Optional[str] = Header(None),
):
    """
    Add `freeMonths` (1, 3 or 5) of free Enterprise access to a partner.

    Extends from whichever is later, the current expiry or now, so extending
    early never shortens the access and extending a lapsed partner starts
    from today. Superadmin only, same as creating the partner.
    """
    payload = await require_admin(authorization)
    if payload.get("account_type") != "superadmin":
        raise HTTPException(status_code=403, detail="Only superadmins can extend partner access")

    free_months = _partner_free_months(body or {})

    pool = await pg.get_pool()
    target = await pool.fetchrow(
        "SELECT id, account_type FROM users WHERE id = $1", user_id
    )
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target["account_type"] != "partner":
        raise HTTPException(status_code=400, detail="Free access can only be extended for partner accounts")

    row = await pool.fetchrow(
        """
        INSERT INTO user_tiers (user_id, tier, features, expires_at, updated_at)
        VALUES ($1, $2, $3::jsonb, now() + make_interval(months => $4), now())
        ON CONFLICT (user_id) DO UPDATE
           SET tier = EXCLUDED.tier,
               features = EXCLUDED.features,
               expires_at = GREATEST(COALESCE(user_tiers.expires_at, now()), now())
                            + make_interval(months => $4),
               lapse_warned_at = NULL,
               updated_at = now()
        RETURNING expires_at
        """,
        user_id, PARTNER_TIER, json.dumps(PARTNER_FEATURES), free_months,
    )

    return {"success": True, "partnerExpiresAt": row["expires_at"].isoformat()}


# ── PATCH /api/v1/admin/users/{id}/seat-limit ────────────────────────────────


@router.patch("/users/{user_id}/seat-limit")
async def set_seat_limit(
    user_id: str,
    body: dict,
    authorization: Optional[str] = Header(None),
):
    """
    Set an account's contracted seat count (Enterprise deals), or clear it
    with `seatLimit: null` to fall back to the plan default. Superadmin only:
    seats are part of what the customer pays for.
    """
    payload = await require_admin(authorization)
    if payload.get("account_type") != "superadmin":
        raise HTTPException(status_code=403, detail="Only superadmins can change seat limits")

    seats = (body or {}).get("seatLimit")
    if seats is not None and (isinstance(seats, bool) or not isinstance(seats, int) or seats < 1 or seats > 10000):
        raise HTTPException(status_code=400, detail="seatLimit must be a whole number from 1 to 10000, or null")

    pool = await pg.get_pool()
    row = await pool.fetchrow(
        "UPDATE user_tiers SET seat_limit = $1, updated_at = now() WHERE user_id = $2 RETURNING tier",
        seats, user_id,
    )
    if not row:
        raise HTTPException(status_code=400, detail="This account has no plan to attach seats to")

    return {"success": True, "seatLimit": tiers.seat_limit(row["tier"], seats)}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _partner_free_months(body: dict) -> int:
    """Validate the free-access duration the admin picked for a partner."""
    months = body.get("freeMonths")
    # bool is an int subclass; `true` must not read as one month.
    if isinstance(months, bool) or months not in PARTNER_FREE_MONTH_OPTIONS:
        options = ", ".join(str(m) for m in PARTNER_FREE_MONTH_OPTIONS)
        raise HTTPException(status_code=400, detail=f"freeMonths must be one of: {options}")
    return months


def _generate_password(length: int = 16) -> str:
    """Generate a strong random password (letters, digits, symbols)."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()_+-="
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _live_plan(account_type: Optional[str], live_tier: Optional[str], features=None) -> str:
    """
    The plan to show in the admin user list: a live subscription, else a
    one-time purchase (blueprint outranks snapshot), else `free` = not
    activated. A partner's snapshot/blueprint features come with its free
    period, so they don't count once that lapses. Superadmins hold Enterprise.
    """
    if account_type == "superadmin":
        return "enterprise"
    plan = tiers.account_tier(live_tier)
    if plan != tiers.FREE_TIER or account_type == "partner":
        return plan
    if isinstance(features, str):
        try:
            features = json.loads(features)
        except ValueError:
            features = None
    bought = set(features or [])
    for feature in ("blueprint", "snapshot"):
        if feature in bought:
            return feature
    return tiers.FREE_TIER


def _get_credits_max(account_type: str) -> int:
    """Return max credits based on account type."""
    return {
        "free": 10,
        "snapshot": 50,
        "blueprint": 100,
        "enterprise": 2000,
        "superadmin": 2000,
        "admin": 500,
        "partner": 3000,  # Enterprise allowance, see PARTNER_TIER
    }.get(account_type, 10)

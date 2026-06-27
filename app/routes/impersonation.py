"""
Impersonation API endpoints — session lifecycle management.

Handles starting and ending impersonation sessions for superadmin users.
All endpoints require active superadmin authentication.
"""

import logging
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Header, Query, Request, Response

from app.database import pg_service as pg
from app.models.impersonation import (
    ImpersonationStartRequest,
    ImpersonationStartResponse,
    ImpersonationStatusResponse,
    ImpersonationHistoryEntry,
)
from app.services.impersonation_session_manager import ImpersonationSessionManager
from app.services.impersonation_audit_logger import ImpersonationAuditLogger
from app.services.impersonation_token_service import ImpersonationTokenService

logger = logging.getLogger(__name__)

# Cookie configuration for the impersonation token
IMPERSONATION_COOKIE_CONFIG = {
    "key": "impersonation_token",
    "httponly": True,
    "secure": True,
    "samesite": "lax",
    "domain": ".avry.io",
    "path": "/",
    "max_age": 3600,
}

# Create router
router = APIRouter(prefix="/api/v1/impersonation", tags=["impersonation"])

# Initialize services
session_manager = ImpersonationSessionManager()
audit_logger = ImpersonationAuditLogger()
token_service = ImpersonationTokenService()


# ── Auth dependency ───────────────────────────────────────────────────────────

import os
import jwt

JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"


async def get_admin_user_id(authorization: Optional[str] = Header(None)) -> str:
    """
    Extract and validate admin user from the Authorization header.

    Verifies the bearer token, then checks that the user is a superadmin
    with impersonation permission.

    Returns:
        The admin's user_id string.

    Raises:
        HTTPException 401: If token is missing or invalid.
        HTTPException 403: If user is not a superadmin.
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

    # Verify the user is a superadmin (the session manager will do deeper validation,
    # but we gate early here for clear error messages)
    account_type = payload.get("account_type")
    if account_type != "superadmin":
        raise HTTPException(status_code=403, detail="Impersonation requires superadmin role")

    return user_id


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/start", response_model=ImpersonationStartResponse)
async def start_impersonation(
    request: Request,
    body: ImpersonationStartRequest,
    response: Response,
    authorization: Optional[str] = Header(None),
):
    """
    Start an impersonation session.

    Validates superadmin auth, creates an impersonation session via the Session Manager,
    sets an HTTP-only cookie with the impersonation token, and returns session metadata.

    Requirements: 2.4, 3.1, 4.5
    """
    admin_user_id = await get_admin_user_id(authorization)

    # Extract request context for audit logging
    admin_ip = request.client.host if request.client else "unknown"
    admin_user_agent = request.headers.get("user-agent", "unknown")

    # Start session (validates permissions, rate limits, concurrent sessions, etc.)
    session = await session_manager.start_session(
        admin_user_id=admin_user_id,
        target_user_id=body.target_user_id,
        access_mode=body.access_mode,
        admin_ip=admin_ip,
        admin_user_agent=admin_user_agent,
    )

    # Log session start event
    await audit_logger.log_session_start(
        session=session,
        admin_ip=admin_ip,
        admin_user_agent=admin_user_agent,
    )

    # Set impersonation token as HTTP-only cookie
    response.set_cookie(
        key=IMPERSONATION_COOKIE_CONFIG["key"],
        value=session.token,
        httponly=IMPERSONATION_COOKIE_CONFIG["httponly"],
        secure=IMPERSONATION_COOKIE_CONFIG["secure"],
        samesite=IMPERSONATION_COOKIE_CONFIG["samesite"],
        domain=IMPERSONATION_COOKIE_CONFIG["domain"],
        path=IMPERSONATION_COOKIE_CONFIG["path"],
        max_age=IMPERSONATION_COOKIE_CONFIG["max_age"],
    )

    # Get target user email for the response
    target_user = await pg.get_user_by_id(body.target_user_id)
    target_email = target_user.get("email", "unknown") if target_user else "unknown"

    logger.info(
        f"Impersonation session started: {session.id} | "
        f"admin={admin_user_id} -> target={body.target_user_id}"
    )

    return ImpersonationStartResponse(
        session_id=session.id,
        target_user_id=session.target_user_id,
        target_email=target_email,
        access_mode=session.access_mode,
        expires_at=session.expires_at,
        started_at=session.started_at,
    )


@router.post("/end")
async def end_impersonation(
    request: Request,
    response: Response,
    authorization: Optional[str] = Header(None),
):
    """
    End an active impersonation session.

    Reads the session from the impersonation token cookie (or from the admin's
    active session), terminates it via Session Manager, clears the impersonation
    cookie, and logs the session_end event.

    Requirements: 8.1, 8.4
    """
    admin_user_id = await get_admin_user_id(authorization)

    # Try to get session_id from the impersonation cookie
    impersonation_token = request.cookies.get("impersonation_token")
    session_id = None

    if impersonation_token:
        token_payload = token_service.validate_token(impersonation_token)
        if token_payload:
            session_id = token_payload.session_id

    # Fallback: look up active session for the admin
    if not session_id:
        active_session = await session_manager.get_active_session(admin_user_id)
        if active_session:
            session_id = active_session.id

    if not session_id:
        raise HTTPException(
            status_code=404,
            detail="No active impersonation session found"
        )

    # Get session details before ending (for audit log)
    session = await session_manager.validate_session(session_id)
    if not session:
        raise HTTPException(
            status_code=404,
            detail="Impersonation session not found or already ended"
        )

    # Verify the admin owns this session
    if session.admin_user_id != admin_user_id:
        raise HTTPException(
            status_code=403,
            detail="You can only end your own impersonation sessions"
        )

    # End the session
    ended = await session_manager.end_session(session_id, "manual")
    if not ended:
        raise HTTPException(
            status_code=500,
            detail="Failed to terminate impersonation session"
        )

    # Calculate session duration for audit log
    now = datetime.now(timezone.utc)
    duration_seconds = int((now - session.started_at).total_seconds())

    # Log session end event
    await audit_logger.log_session_end(
        session_id=session_id,
        reason="manual",
        duration_seconds=duration_seconds,
        total_requests=session.total_requests,
        mutations_attempted=session.mutations_attempted,
        mutations_blocked=session.mutations_blocked,
    )

    # Clear the impersonation token cookie
    response.delete_cookie(
        key=IMPERSONATION_COOKIE_CONFIG["key"],
        httponly=IMPERSONATION_COOKIE_CONFIG["httponly"],
        secure=IMPERSONATION_COOKIE_CONFIG["secure"],
        samesite=IMPERSONATION_COOKIE_CONFIG["samesite"],
        domain=IMPERSONATION_COOKIE_CONFIG["domain"],
        path=IMPERSONATION_COOKIE_CONFIG["path"],
    )

    logger.info(
        f"Impersonation session ended: {session_id} | "
        f"admin={admin_user_id} | duration={duration_seconds}s"
    )

    return {
        "success": True,
        "session_id": session_id,
        "message": "Impersonation session ended successfully",
    }


@router.post("/terminate-sessions")
async def terminate_sessions(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """
    Terminate specific impersonation sessions by ID. Superadmin only.
    
    Accepts a JSON body with { "session_ids": ["id1", "id2", ...] }.
    Only superadmins can terminate any session. Regular admins can only
    terminate their own sessions.
    """
    admin_user_id = await get_admin_user_id(authorization)

    body = await request.json()
    session_ids = body.get("session_ids", [])

    if not session_ids:
        raise HTTPException(status_code=400, detail="session_ids is required")

    pool = await pg.get_pool()
    
    # Terminate each session
    terminated = 0
    for sid in session_ids:
        result = await pool.execute(
            """
            UPDATE impersonation_sessions 
            SET status = 'terminated', ended_at = NOW(), termination_reason = 'admin_force_terminate'
            WHERE id = $1 AND status = 'active'
            """,
            sid,
        )
        if result == "UPDATE 1":
            terminated += 1

    return {
        "success": True,
        "terminated": terminated,
        "total_requested": len(session_ids),
        "message": f"Terminated {terminated} session(s)",
    }


@router.post("/extend")
async def extend_impersonation(
    request: Request,
    response: Response,
    authorization: Optional[str] = Header(None),
):
    """
    Extend an active impersonation session by 30 minutes (max 4 hours total).

    Reads the session from the impersonation token cookie (or from the admin's
    active session), extends it via Session Manager, and updates the cookie max_age.

    Requirements: 3.6
    """
    admin_user_id = await get_admin_user_id(authorization)

    # Try to get session_id from the impersonation cookie
    impersonation_token = request.cookies.get("impersonation_token")
    session_id = None

    if impersonation_token:
        token_payload = token_service.validate_token(impersonation_token)
        if token_payload:
            session_id = token_payload.session_id

    # Fallback: look up active session for the admin
    if not session_id:
        active_session = await session_manager.get_active_session(admin_user_id)
        if active_session:
            session_id = active_session.id

    if not session_id:
        raise HTTPException(
            status_code=404,
            detail="No active impersonation session found"
        )

    # Extend the session (validates active status and max duration internally)
    updated_session = await session_manager.extend_session(session_id)

    # Verify the admin owns this session
    if updated_session.admin_user_id != admin_user_id:
        raise HTTPException(
            status_code=403,
            detail="You can only extend your own impersonation sessions"
        )

    # Calculate new max_age for the cookie based on extended expires_at
    now = datetime.now(timezone.utc)
    new_max_age = int((updated_session.expires_at - now).total_seconds())

    # Update the impersonation token cookie with new max_age
    if impersonation_token:
        response.set_cookie(
            key=IMPERSONATION_COOKIE_CONFIG["key"],
            value=impersonation_token,
            httponly=IMPERSONATION_COOKIE_CONFIG["httponly"],
            secure=IMPERSONATION_COOKIE_CONFIG["secure"],
            samesite=IMPERSONATION_COOKIE_CONFIG["samesite"],
            domain=IMPERSONATION_COOKIE_CONFIG["domain"],
            path=IMPERSONATION_COOKIE_CONFIG["path"],
            max_age=new_max_age,
        )

    logger.info(
        f"Impersonation session extended: {session_id} | "
        f"admin={admin_user_id} | new_expires_at={updated_session.expires_at.isoformat()}"
    )

    return {
        "success": True,
        "session_id": session_id,
        "expires_at": updated_session.expires_at.isoformat(),
        "remaining_seconds": new_max_age,
        "message": "Impersonation session extended successfully",
    }


@router.get("/status", response_model=ImpersonationStatusResponse)
async def get_impersonation_status(
    request: Request,
):
    """
    Get the current impersonation session status.

    Reads the impersonation token from the cookie, validates the session,
    and returns status including remaining time.

    Requirements: 10.4
    """
    impersonation_token = request.cookies.get("impersonation_token")

    if not impersonation_token:
        return ImpersonationStatusResponse(active=False)

    # Validate the token
    token_payload = token_service.validate_token(impersonation_token)
    if not token_payload:
        return ImpersonationStatusResponse(active=False)

    # Validate the session is still active
    session = await session_manager.validate_session(token_payload.session_id)
    if not session:
        return ImpersonationStatusResponse(active=False)

    # Get target user email
    target_user = await pg.get_user_by_id(session.target_user_id)
    target_email = target_user.get("email", "unknown") if target_user else "unknown"

    # Calculate remaining seconds
    now = datetime.now(timezone.utc)
    remaining_seconds = max(0, int((session.expires_at - now).total_seconds()))

    return ImpersonationStatusResponse(
        active=True,
        session_id=session.id,
        target_user_id=session.target_user_id,
        target_email=target_email,
        access_mode=session.access_mode,
        expires_at=session.expires_at,
        remaining_seconds=remaining_seconds,
    )


@router.get("/history", response_model=List[ImpersonationHistoryEntry])
async def get_impersonation_history(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    authorization: Optional[str] = Header(None),
):
    """
    Get impersonation session history for the authenticated admin.

    Returns a paginated list of past impersonation sessions ordered by
    most recent first.

    Requirements: 10.4
    """
    admin_user_id = await get_admin_user_id(authorization)

    entries = await session_manager.get_session_history(
        admin_user_id=admin_user_id,
        limit=limit,
        offset=offset,
    )

    return entries

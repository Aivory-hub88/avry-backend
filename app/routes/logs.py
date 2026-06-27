"""
Logs API endpoints — serves execution log data from the PostgreSQL audit_logs table.

Provides real audit log data for the admin dashboard Execution Logs page,
including impersonation events with source label "impersonation-monitor".
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional, List

import jwt
from fastapi import APIRouter, HTTPException, Header, Query

from app.database import pg_service as pg

logger = logging.getLogger(__name__)

# JWT config (same as used in impersonation routes)
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"

# Create router
router = APIRouter(prefix="/api/v1/admin", tags=["admin-logs"])


# ── Auth dependency ───────────────────────────────────────────────────────────


async def get_admin_user_id(authorization: Optional[str] = Header(None)) -> str:
    """
    Extract and validate admin user from the Authorization header.

    Verifies the bearer token and checks that the user is a superadmin.

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

    account_type = payload.get("account_type")
    if account_type != "superadmin":
        raise HTTPException(status_code=403, detail="Admin access required")

    return user_id


# ── Helpers ───────────────────────────────────────────────────────────────────


def _determine_log_level(changes: dict) -> str:
    """
    Determine the log level for an impersonation event based on its sub_action.

    - session_start / session_end → "info"
    - mutation_blocked → "warn"
    - request events with error status codes → "error"
    - request events with success status codes → "info"
    """
    sub_action = changes.get("sub_action", "")

    if sub_action == "mutation_blocked":
        return "warn"

    if sub_action == "request":
        status_code = changes.get("response_status_code", 200)
        if isinstance(status_code, int) and status_code >= 500:
            return "error"
        if isinstance(status_code, int) and status_code >= 400:
            return "warn"

    return "info"


def _build_log_message(changes: dict) -> str:
    """
    Build a human-readable log message from the JSONB event payload.
    """
    sub_action = changes.get("sub_action", "unknown")

    if sub_action == "session_start":
        admin = changes.get("admin_user_id", "unknown")
        target = changes.get("target_user_id", "unknown")
        mode = changes.get("access_mode", "unknown")
        return f"Impersonation session started: admin={admin} → target={target} (mode={mode})"

    if sub_action == "session_end":
        reason = changes.get("termination_reason", "unknown")
        duration = changes.get("duration_seconds", 0)
        total_requests = changes.get("total_requests_made", 0)
        return (
            f"Impersonation session ended: reason={reason}, "
            f"duration={duration}s, requests={total_requests}"
        )

    if sub_action == "mutation_blocked":
        method = changes.get("method", "?")
        endpoint = changes.get("endpoint", "?")
        reason = changes.get("reason", "unknown")
        return f"Mutation blocked: {method} {endpoint} — {reason}"

    if sub_action == "request":
        method = changes.get("method", "?")
        path = changes.get("request_path", changes.get("endpoint", "?"))
        status = changes.get("response_status_code", "?")
        time_ms = changes.get("response_time_ms", "?")
        return f"Request: {method} {path} → {status} ({time_ms}ms)"

    return f"Impersonation event: {sub_action}"


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.get("/logs")
async def get_execution_logs(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    level: Optional[str] = Query(default=None, description="Filter by level: info, warn, error"),
    source: Optional[str] = Query(default=None, description="Filter by source label"),
    authorization: Optional[str] = Header(None),
):
    """
    Get execution logs for the admin dashboard.

    Queries the PostgreSQL `audit_logs` table for impersonation events
    (action='impersonation') and returns structured log entries with
    source label "impersonation-monitor".

    Requirements: 6.2, 6.3
    """
    await get_admin_user_id(authorization)

    try:
        pool = await pg.get_pool()
    except RuntimeError:
        # PostgreSQL pool not available — return empty logs
        return {"logs": []}

    # Build the query to fetch impersonation events from audit_logs
    query = """
        SELECT id, user_id, action, entity_type, entity_id, changes, ip_address, user_agent, created_at
        FROM audit_logs
        WHERE action = 'impersonation'
        ORDER BY created_at DESC
        LIMIT $1 OFFSET $2
    """

    try:
        rows = await pool.fetch(query, limit, offset)
    except Exception as e:
        logger.error(f"Failed to query audit_logs: {e}")
        return {"logs": []}

    logs: List[dict] = []
    for row in rows:
        # Parse the JSONB changes column
        changes_raw = row["changes"]
        if isinstance(changes_raw, str):
            try:
                changes = json.loads(changes_raw)
            except (json.JSONDecodeError, TypeError):
                changes = {}
        elif isinstance(changes_raw, dict):
            changes = changes_raw
        else:
            changes = {}

        log_level = _determine_log_level(changes)
        message = _build_log_message(changes)

        # Apply level filter if specified
        if level and level != "all" and log_level != level:
            continue

        # Apply source filter if specified (only "impersonation-monitor" for now)
        source_label = "impersonation-monitor"
        if source and source != source_label:
            continue

        # Format the timestamp
        created_at = row["created_at"]
        if isinstance(created_at, datetime):
            timestamp = created_at.isoformat()
        else:
            timestamp = str(created_at)

        log_entry = {
            "id": row["id"],
            "timestamp": timestamp,
            "level": log_level,
            "source": source_label,
            "message": message,
            "details": {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "sub_action": changes.get("sub_action"),
                "session_id": changes.get("session_id"),
                "admin_user_id": changes.get("admin_user_id") or row["user_id"],
                "target_user_id": changes.get("target_user_id"),
                "ip_address": row["ip_address"],
                "user_agent": row["user_agent"],
                **{k: v for k, v in changes.items() if k not in (
                    "sub_action", "session_id", "admin_user_id", "target_user_id"
                )},
            },
        }

        logs.append(log_entry)

    return {"logs": logs}

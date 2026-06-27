"""
Impersonation Session Manager — Core business logic for impersonation session lifecycle.

Handles session creation, validation, extension, termination, and rate limiting.
All session state is persisted in the PostgreSQL `impersonation_sessions` table.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from dataclasses import dataclass

from fastapi import HTTPException

from app.database import pg_service as pg
from app.models.impersonation import ImpersonationHistoryEntry
from app.services.impersonation_token_service import ImpersonationTokenService
from app.utils.id_generator import generate_id

logger = logging.getLogger(__name__)

# Configuration constants
MAX_SESSIONS_PER_24H = 10
SESSION_TTL_MINUTES = 60
EXTENSION_MINUTES = 30
MAX_SESSION_DURATION_HOURS = 4


@dataclass
class ImpersonationSession:
    """Represents an active or historical impersonation session."""
    id: str
    admin_user_id: str
    target_user_id: str
    access_mode: str
    status: str
    started_at: datetime
    expires_at: datetime
    ended_at: Optional[datetime] = None
    termination_reason: Optional[str] = None
    total_requests: int = 0
    mutations_attempted: int = 0
    mutations_blocked: int = 0
    token: Optional[str] = None


class ImpersonationSessionManager:
    """Manages impersonation session lifecycle — creation, validation, extension, and termination."""

    def __init__(self):
        self.token_service = ImpersonationTokenService()

    async def start_session(
        self,
        admin_user_id: str,
        target_user_id: str,
        access_mode: str,
        admin_ip: str,
        admin_user_agent: str,
    ) -> ImpersonationSession:
        """
        Start a new impersonation session.

        Validates:
            - Admin is a superadmin with impersonation_permission
            - Target user is not a superadmin
            - Admin has no active concurrent session
            - Rate limit not exceeded (max 10 sessions per 24h)

        Args:
            admin_user_id: The superadmin's user ID.
            target_user_id: The user to impersonate.
            access_mode: "read_only" or "full_access".
            admin_ip: IP address of the admin making the request.
            admin_user_agent: User-Agent string of the admin's browser.

        Returns:
            ImpersonationSession with token attached.

        Raises:
            HTTPException: On validation failures (403, 400, 409, 429).
        """
        # 1. Validate admin is superadmin
        admin_user = await pg.get_user_by_id(admin_user_id)
        if not admin_user:
            raise HTTPException(status_code=403, detail="Impersonation requires superadmin role")

        if admin_user.get("account_type") != "superadmin":
            raise HTTPException(status_code=403, detail="Impersonation requires superadmin role")

        # Auto-grant impersonation_permission on first use (consent modal is the acknowledgement)
        if not admin_user.get("impersonation_permission"):
            pool = await pg.get_pool()
            await pool.execute(
                "UPDATE users SET impersonation_permission = true WHERE id = $1",
                admin_user_id,
            )

        # 2. Validate target is not superadmin
        target_user = await pg.get_user_by_id(target_user_id)
        if not target_user:
            raise HTTPException(status_code=404, detail="Target user does not exist")

        if target_user.get("account_type") == "superadmin":
            raise HTTPException(status_code=400, detail="Cannot impersonate superadmin accounts")

        # 3. Validate no active concurrent session for this admin
        active_session = await self.get_active_session(admin_user_id)
        if active_session is not None:
            raise HTTPException(
                status_code=409,
                detail="Active impersonation session already exists"
            )

        # 4. Check rate limit (max 10 sessions per 24h rolling window)
        rate_limit_ok = await self.check_rate_limit(admin_user_id)
        if not rate_limit_ok:
            raise HTTPException(
                status_code=429,
                detail="Maximum 10 impersonation sessions per 24 hours reached"
            )

        # 5. Create session
        session_id = generate_id("imp_session")
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=SESSION_TTL_MINUTES)

        # 6. Create impersonation token
        token = self.token_service.create_token(
            admin_user_id=admin_user_id,
            target_user_id=target_user_id,
            access_mode=access_mode,
            session_id=session_id,
        )

        # 7. Insert session record into impersonation_sessions table
        pool = await pg.get_pool()
        await pool.execute(
            """
            INSERT INTO impersonation_sessions
                (id, admin_user_id, target_user_id, access_mode, status, started_at, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            session_id,
            admin_user_id,
            target_user_id,
            access_mode,
            "active",
            now,
            expires_at,
        )

        logger.info(
            f"Impersonation session started: {session_id} | "
            f"admin={admin_user_id} -> target={target_user_id} | "
            f"mode={access_mode} | ip={admin_ip}"
        )

        return ImpersonationSession(
            id=session_id,
            admin_user_id=admin_user_id,
            target_user_id=target_user_id,
            access_mode=access_mode,
            status="active",
            started_at=now,
            expires_at=expires_at,
            token=token,
        )

    async def get_active_session(self, admin_user_id: str) -> Optional[ImpersonationSession]:
        """
        Get the currently active impersonation session for an admin user.

        Args:
            admin_user_id: The admin's user ID.

        Returns:
            ImpersonationSession if one is active, None otherwise.
        """
        pool = await pg.get_pool()
        row = await pool.fetchrow(
            """
            SELECT id, admin_user_id, target_user_id, access_mode, status,
                   started_at, expires_at, ended_at, termination_reason,
                   total_requests, mutations_attempted, mutations_blocked
            FROM impersonation_sessions
            WHERE admin_user_id = $1 AND status = 'active'
            ORDER BY started_at DESC
            LIMIT 1
            """,
            admin_user_id,
        )
        if not row:
            return None

        return ImpersonationSession(
            id=row["id"],
            admin_user_id=row["admin_user_id"],
            target_user_id=row["target_user_id"],
            access_mode=row["access_mode"],
            status=row["status"],
            started_at=row["started_at"],
            expires_at=row["expires_at"],
            ended_at=row["ended_at"],
            termination_reason=row["termination_reason"],
            total_requests=row["total_requests"],
            mutations_attempted=row["mutations_attempted"],
            mutations_blocked=row["mutations_blocked"],
        )

    async def check_rate_limit(self, admin_user_id: str) -> bool:
        """
        Check if the admin has exceeded the rate limit (max 10 sessions per 24h).

        Args:
            admin_user_id: The admin's user ID.

        Returns:
            True if the admin can start a new session, False if rate limit exceeded.
        """
        pool = await pg.get_pool()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        count = await pool.fetchval(
            """
            SELECT COUNT(*)
            FROM impersonation_sessions
            WHERE admin_user_id = $1 AND started_at >= $2
            """,
            admin_user_id,
            cutoff,
        )
        return count < MAX_SESSIONS_PER_24H

    async def end_session(self, session_id: str, reason: str) -> bool:
        """
        Terminate an impersonation session.

        Updates the session status to 'terminated', sets ended_at to NOW(),
        and records the termination reason.

        Args:
            session_id: The impersonation session ID.
            reason: The reason for termination (e.g., "manual", "expired", "error").

        Returns:
            True if the session was successfully terminated, False if not found or already ended.
        """
        pool = await pg.get_pool()
        now = datetime.now(timezone.utc)
        result = await pool.execute(
            """
            UPDATE impersonation_sessions
            SET status = 'terminated',
                ended_at = $2,
                termination_reason = $3,
                updated_at = $2
            WHERE id = $1 AND status = 'active'
            """,
            session_id,
            now,
            reason,
        )
        # asyncpg returns a string like "UPDATE 1" or "UPDATE 0"
        rows_affected = int(result.split(" ")[-1])
        if rows_affected > 0:
            logger.info(
                f"Impersonation session ended: {session_id} | reason={reason}"
            )
            return True
        return False

    async def extend_session(self, session_id: str) -> ImpersonationSession:
        """
        Extend an impersonation session by 30 minutes, capped at 4 hours total.

        New expiry = min(current_expires_at + 30 min, started_at + 4 hours).
        If the session has already reached the 4-hour maximum, raises HTTP 400.

        Args:
            session_id: The impersonation session ID to extend.

        Returns:
            Updated ImpersonationSession with new expires_at.

        Raises:
            HTTPException 404: If session not found or not active.
            HTTPException 400: If session has reached maximum duration of 4 hours.
        """
        pool = await pg.get_pool()

        # Fetch the current session
        row = await pool.fetchrow(
            """
            SELECT id, admin_user_id, target_user_id, access_mode, status,
                   started_at, expires_at, ended_at, termination_reason,
                   total_requests, mutations_attempted, mutations_blocked
            FROM impersonation_sessions
            WHERE id = $1 AND status = 'active'
            """,
            session_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Active session not found")

        started_at = row["started_at"]
        current_expires_at = row["expires_at"]
        max_expires_at = started_at + timedelta(hours=MAX_SESSION_DURATION_HOURS)

        # Check if already at maximum duration
        if current_expires_at >= max_expires_at:
            raise HTTPException(
                status_code=400,
                detail="Session has reached maximum duration of 4 hours",
            )

        # Calculate new expiry: min(current + 30min, started + 4h)
        new_expires_at = min(
            current_expires_at + timedelta(minutes=EXTENSION_MINUTES),
            max_expires_at,
        )

        await pool.execute(
            """
            UPDATE impersonation_sessions
            SET expires_at = $2, updated_at = $3
            WHERE id = $1
            """,
            session_id,
            new_expires_at,
            datetime.now(timezone.utc),
        )

        logger.info(
            f"Impersonation session extended: {session_id} | "
            f"new_expires_at={new_expires_at.isoformat()}"
        )

        return ImpersonationSession(
            id=row["id"],
            admin_user_id=row["admin_user_id"],
            target_user_id=row["target_user_id"],
            access_mode=row["access_mode"],
            status=row["status"],
            started_at=row["started_at"],
            expires_at=new_expires_at,
            ended_at=row["ended_at"],
            termination_reason=row["termination_reason"],
            total_requests=row["total_requests"],
            mutations_attempted=row["mutations_attempted"],
            mutations_blocked=row["mutations_blocked"],
        )

    async def validate_session(self, session_id: str) -> Optional[ImpersonationSession]:
        """
        Validate that an impersonation session exists, is active, and has not expired.

        If the session is active but expired (expires_at < NOW), it is automatically
        terminated with reason "expired" and None is returned.

        Args:
            session_id: The impersonation session ID to validate.

        Returns:
            ImpersonationSession if valid and active, None otherwise.
        """
        pool = await pg.get_pool()
        row = await pool.fetchrow(
            """
            SELECT id, admin_user_id, target_user_id, access_mode, status,
                   started_at, expires_at, ended_at, termination_reason,
                   total_requests, mutations_attempted, mutations_blocked
            FROM impersonation_sessions
            WHERE id = $1
            """,
            session_id,
        )
        if not row:
            return None

        if row["status"] != "active":
            return None

        # Check if expired
        now = datetime.now(timezone.utc)
        if row["expires_at"] <= now:
            # Auto-terminate expired session
            await self.end_session(session_id, "expired")
            return None

        return ImpersonationSession(
            id=row["id"],
            admin_user_id=row["admin_user_id"],
            target_user_id=row["target_user_id"],
            access_mode=row["access_mode"],
            status=row["status"],
            started_at=row["started_at"],
            expires_at=row["expires_at"],
            ended_at=row["ended_at"],
            termination_reason=row["termination_reason"],
            total_requests=row["total_requests"],
            mutations_attempted=row["mutations_attempted"],
            mutations_blocked=row["mutations_blocked"],
        )

    async def get_session_history(
        self,
        admin_user_id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[ImpersonationHistoryEntry]:
        """
        Get impersonation session history with pagination.

        Args:
            admin_user_id: Optional filter by admin user ID. If None, returns all sessions.
            limit: Maximum number of entries to return (default 20).
            offset: Number of entries to skip for pagination (default 0).

        Returns:
            List of ImpersonationHistoryEntry objects ordered by started_at descending.
        """
        pool = await pg.get_pool()

        if admin_user_id:
            rows = await pool.fetch(
                """
                SELECT s.id, s.admin_user_id, s.target_user_id, s.access_mode,
                       s.status, s.started_at, s.ended_at, s.total_requests,
                       u.email AS target_email
                FROM impersonation_sessions s
                LEFT JOIN users u ON u.id = s.target_user_id
                WHERE s.admin_user_id = $1
                ORDER BY s.started_at DESC
                LIMIT $2 OFFSET $3
                """,
                admin_user_id,
                limit,
                offset,
            )
        else:
            rows = await pool.fetch(
                """
                SELECT s.id, s.admin_user_id, s.target_user_id, s.access_mode,
                       s.status, s.started_at, s.ended_at, s.total_requests,
                       u.email AS target_email
                FROM impersonation_sessions s
                LEFT JOIN users u ON u.id = s.target_user_id
                ORDER BY s.started_at DESC
                LIMIT $1 OFFSET $2
                """,
                limit,
                offset,
            )

        entries = []
        for row in rows:
            duration_seconds = None
            if row["ended_at"] and row["started_at"]:
                duration_seconds = int(
                    (row["ended_at"] - row["started_at"]).total_seconds()
                )

            entries.append(
                ImpersonationHistoryEntry(
                    session_id=row["id"],
                    admin_user_id=row["admin_user_id"],
                    target_user_id=row["target_user_id"],
                    target_email=row["target_email"] or "unknown",
                    access_mode=row["access_mode"],
                    status=row["status"],
                    started_at=row["started_at"],
                    ended_at=row["ended_at"],
                    duration_seconds=duration_seconds,
                    total_requests=row["total_requests"],
                )
            )

        return entries

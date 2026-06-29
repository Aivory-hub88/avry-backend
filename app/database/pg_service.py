"""
PostgreSQL database service for user auth.

Handles users, sessions and auth-tokens using asyncpg.
The rest of the data (diagnostics, snapshots, blueprints, payments) still
lives in the JSON file store — db_service.py is unchanged.
"""

import asyncpg
import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level pool — initialised once in lifespan
_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() first")
    return _pool


async def init_pool() -> None:
    """Create the asyncpg connection pool and run migrations."""
    global _pool
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.warning("DATABASE_URL not set — auth will fall back to file storage")
        return

    try:
        _pool = await asyncpg.create_pool(
            dsn=database_url,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        await _run_migrations()
        logger.info("[✓] PostgreSQL pool ready (auth)")
    except Exception as e:
        logger.error(f"[!] Could not connect to PostgreSQL: {e}")
        _pool = None


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def is_available() -> bool:
    """True if the PG pool is up and working."""
    if _pool is None:
        return False
    try:
        async with _pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
-- Users table (TEXT id to match existing string IDs like "user_xxx")
CREATE TABLE IF NOT EXISTS users (
    id           TEXT PRIMARY KEY,
    email        VARCHAR(255) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    account_type VARCHAR(50) NOT NULL DEFAULT 'free',
    company_name TEXT,
    is_active    BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users (email);

-- Sessions table — uses 'refresh_token' column; add it if the table was
-- created by the old schema which used 'session_token'.
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    refresh_token TEXT UNIQUE,
    expires_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Migrate old 'session_token' column to 'refresh_token' if needed
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='sessions' AND column_name='session_token'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='sessions' AND column_name='refresh_token'
    ) THEN
        ALTER TABLE sessions ADD COLUMN refresh_token TEXT;
        UPDATE sessions SET refresh_token = session_token;
    END IF;

    -- Ensure refresh_token column exists (add if totally missing)
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='sessions' AND column_name='refresh_token'
    ) THEN
        ALTER TABLE sessions ADD COLUMN refresh_token TEXT;
    END IF;

    -- Ensure expires_at column exists
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='sessions' AND column_name='expires_at'
    ) THEN
        ALTER TABLE sessions ADD COLUMN expires_at TIMESTAMPTZ;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions (user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_refresh ON sessions (refresh_token) WHERE refresh_token IS NOT NULL;

-- Audit logs table (for impersonation and general audit events)
CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users(id),
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    changes JSONB,
    ip_address VARCHAR(45),
    user_agent TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs(action);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at);

-- Impersonation sessions table
CREATE TABLE IF NOT EXISTS impersonation_sessions (
    id TEXT PRIMARY KEY,
    admin_user_id TEXT NOT NULL REFERENCES users(id),
    target_user_id TEXT NOT NULL REFERENCES users(id),
    access_mode VARCHAR(20) NOT NULL DEFAULT 'read_only',
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    termination_reason VARCHAR(50),
    total_requests INTEGER NOT NULL DEFAULT 0,
    mutations_attempted INTEGER NOT NULL DEFAULT 0,
    mutations_blocked INTEGER NOT NULL DEFAULT 0,
    pages_visited JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_imp_sessions_admin ON impersonation_sessions(admin_user_id);
CREATE INDEX IF NOT EXISTS idx_imp_sessions_status ON impersonation_sessions(status);
CREATE INDEX IF NOT EXISTS idx_imp_sessions_started ON impersonation_sessions(started_at);

-- Add impersonation_permission to users table
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='users' AND column_name='impersonation_permission'
    ) THEN
        ALTER TABLE users ADD COLUMN impersonation_permission BOOLEAN DEFAULT false;
    END IF;
END $$;
"""


async def _run_migrations() -> None:
    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
    logger.info("[✓] Auth schema ready")


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------

async def get_user_by_email(email: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, email, password_hash, account_type, company_name, full_name, username, "
        "is_active, impersonation_permission, created_at, updated_at "
        "FROM users WHERE email = $1",
        email,
    )
    return dict(row) if row else None


async def get_user_by_id(user_id: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, email, password_hash, account_type, company_name, full_name, username, "
        "is_active, impersonation_permission, created_at, updated_at "
        "FROM users WHERE id = $1",
        user_id,
    )
    return dict(row) if row else None


async def email_exists(email: str) -> bool:
    pool = await get_pool()
    val = await pool.fetchval("SELECT 1 FROM users WHERE email = $1", email)
    return val is not None


async def insert_user(
    user_id: str,
    email: str,
    password_hash: str,
    account_type: str = "free",
    company_name: Optional[str] = None,
) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO users (id, email, password_hash, account_type, company_name)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id, email, account_type, company_name, created_at, updated_at
        """,
        user_id, email, password_hash, account_type, company_name,
    )
    return dict(row)


async def update_user_account_type(user_id: str, account_type: str) -> bool:
    pool = await get_pool()
    result = await pool.execute(
        "UPDATE users SET account_type=$1, updated_at=NOW() WHERE id=$2",
        account_type, user_id,
    )
    return result == "UPDATE 1"


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------

async def insert_session(
    session_id: str,
    user_id: str,
    refresh_token: str,
    expires_at,
) -> None:
    pool = await get_pool()
    # Ensure expires_at is timezone-aware
    if hasattr(expires_at, 'tzinfo') and expires_at.tzinfo is None:
        from datetime import timezone
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    await pool.execute(
        """
        INSERT INTO sessions (id, user_id, refresh_token, expires_at)
        VALUES ($1, $2, $3, $4)
        """,
        session_id, user_id, refresh_token, expires_at,
    )


async def get_session_by_id(session_id: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, user_id, refresh_token, expires_at, created_at "
        "FROM sessions WHERE id = $1",
        session_id,
    )
    return dict(row) if row else None


async def delete_session(session_id: str) -> bool:
    pool = await get_pool()
    result = await pool.execute("DELETE FROM sessions WHERE id=$1", session_id)
    return result == "DELETE 1"


async def get_user_from_refresh_token(refresh_token: str) -> Optional[dict]:
    """Return user row for a given refresh token."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT u.id, u.email, u.account_type, u.company_name,
               u.created_at, u.updated_at, s.id AS session_id
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.refresh_token = $1
        """,
        refresh_token,
    )
    return dict(row) if row else None

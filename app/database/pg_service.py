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

-- ── Templates & Agents (shared with user/admin dashboards) ─────────────
CREATE TABLE IF NOT EXISTS templates (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT,
    category      TEXT DEFAULT 'general',
    tags          TEXT[] DEFAULT '{}',
    apps          TEXT[] DEFAULT '{}',
    status        TEXT DEFAULT 'draft',
    uses_count    INTEGER DEFAULT 0,
    workflow_json JSONB DEFAULT '{}'::jsonb,
    created_by    TEXT,
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_templates_status ON templates(status);
CREATE INDEX IF NOT EXISTS idx_templates_category ON templates(category);

CREATE TABLE IF NOT EXISTS agents (
    agent_id     TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    name         TEXT NOT NULL,
    type         TEXT,
    status       TEXT DEFAULT 'inactive',
    total_runs   INTEGER DEFAULT 0,
    success_rate NUMERIC DEFAULT 0,
    last_run_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agents_user_id ON agents(user_id);

INSERT INTO templates (id, name, description, category, tags, apps, status, uses_count) VALUES
  ('tpl-wa-autoreply', 'Simple WhatsApp Auto Reply (AI)', 'Automatically respond to incoming WhatsApp messages using Aivory AI.', 'Communication', ARRAY['whatsapp','ai'], ARRAY['whatsapp'], 'active', 841),
  ('tpl-support-escalation', 'Customer Support Escalation', 'Extract sentiment from support tickets and escalate angry customers to a human agent.', 'Customer', ARRAY['support','sentiment'], ARRAY['zendesk','slack'], 'active', 575),
  ('tpl-lead-scoring', 'Lead Scoring Pipeline', 'Score and route inbound leads automatically.', 'sales', ARRAY['leads','scoring'], ARRAY['hubspot'], 'active', 0),
  ('tpl-email-campaign', 'Email Campaign Automation', 'Automated email drip sequences.', 'marketing', ARRAY['email','drip'], ARRAY['gmail'], 'active', 0)
ON CONFLICT (id) DO NOTHING;

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
        "SELECT id, email, password_hash, account_type, company_name, "
        "is_active, created_at, updated_at FROM users WHERE email = $1",
        email,
    )
    return dict(row) if row else None


async def get_user_by_id(user_id: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, email, password_hash, account_type, company_name, "
        "is_active, created_at, updated_at FROM users WHERE id = $1",
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


# ===========================================================================
# Templates & Agents (shared with user/admin dashboards)
# ===========================================================================
import json as _json


def _row_to_template(row) -> dict:
    d = dict(row)
    wf = d.get("workflow_json")
    if isinstance(wf, str):
        try:
            d["workflow_json"] = _json.loads(wf)
        except Exception:
            d["workflow_json"] = {}
    return d


async def list_templates(status: Optional[str] = None) -> list:
    pool = await get_pool()
    if status:
        rows = await pool.fetch("SELECT * FROM templates WHERE status=$1 ORDER BY created_at DESC", status)
    else:
        rows = await pool.fetch("SELECT * FROM templates ORDER BY created_at DESC")
    return [_row_to_template(r) for r in rows]


async def get_template(tid: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM templates WHERE id=$1", tid)
    return _row_to_template(row) if row else None


async def insert_template(data: dict) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        """INSERT INTO templates
           (id,name,description,category,tags,apps,status,uses_count,workflow_json,created_by)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10) RETURNING *""",
        data["id"], data["name"], data.get("description"), data.get("category", "general"),
        list(data.get("tags") or []), list(data.get("apps") or []),
        data.get("status", "draft"), int(data.get("uses_count", 0) or 0),
        _json.dumps(data.get("workflow_json") or {}), data.get("created_by"),
    )
    return _row_to_template(row)


async def update_template(tid: str, data: dict) -> Optional[dict]:
    pool = await get_pool()
    wf = data.get("workflow_json")
    row = await pool.fetchrow(
        """UPDATE templates SET
             name=COALESCE($2,name), description=COALESCE($3,description),
             category=COALESCE($4,category), tags=COALESCE($5,tags), apps=COALESCE($6,apps),
             status=COALESCE($7,status),
             workflow_json=COALESCE($8::jsonb,workflow_json),
             updated_at=now()
           WHERE id=$1 RETURNING *""",
        tid, data.get("name"), data.get("description"), data.get("category"),
        list(data["tags"]) if data.get("tags") is not None else None,
        list(data["apps"]) if data.get("apps") is not None else None,
        data.get("status"),
        _json.dumps(wf) if wf is not None else None,
    )
    return _row_to_template(row) if row else None


async def delete_template(tid: str) -> bool:
    pool = await get_pool()
    res = await pool.execute("DELETE FROM templates WHERE id=$1", tid)
    return res.split()[-1] != "0"


async def increment_template_uses(tid: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "UPDATE templates SET uses_count=uses_count+1 WHERE id=$1 RETURNING *", tid)
    return _row_to_template(row) if row else None


async def list_agents(user_id: Optional[str] = None) -> list:
    pool = await get_pool()
    if user_id:
        rows = await pool.fetch("SELECT * FROM agents WHERE user_id=$1 ORDER BY created_at DESC", user_id)
    else:
        rows = await pool.fetch("SELECT * FROM agents ORDER BY created_at DESC")
    return [dict(r) for r in rows]


async def get_agent(aid: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM agents WHERE agent_id=$1", aid)
    return dict(row) if row else None


async def upsert_agent(data: dict) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        """INSERT INTO agents
           (agent_id,user_id,name,type,status,total_runs,success_rate,last_run_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
           ON CONFLICT (agent_id) DO UPDATE SET
             name=EXCLUDED.name, type=EXCLUDED.type, status=EXCLUDED.status,
             total_runs=EXCLUDED.total_runs, success_rate=EXCLUDED.success_rate,
             last_run_at=EXCLUDED.last_run_at
           RETURNING *""",
        data["agent_id"], data["user_id"], data["name"], data.get("type"),
        data.get("status", "inactive"), int(data.get("total_runs", 0) or 0),
        float(data.get("success_rate", 0) or 0), data.get("last_run_at"),
    )
    return dict(row)

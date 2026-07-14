"""
Agent identity profiles — per-user customization of the prebuilt deployable agents.

Each user can give every agent type its own identity (agent name, business
name, tone, knowledge/FAQ, extra instructions). The bridge runtime injects the
profile into the system prompt PER REQUEST, so different operators' identities
can never collide: there is no shared mutable identity state anywhere.

Values are operator-authored and end up inside an LLM prompt, so they are
treated as untrusted: length-capped and control-char-stripped here, and
wrapped in a non-overridable "operator configuration" data block by the bridge.

Table: product.agent_profiles (avry-postgres).

Dashboard-facing (JWT auth):
    GET /api/v1/agent-profiles                   -> all of the caller's profiles
    GET /api/v1/agent-profiles/{agent_type}      -> one profile (or defaults)
    PUT /api/v1/agent-profiles/{agent_type}      -> upsert
    DELETE /api/v1/agent-profiles/{agent_type}   -> reset to default identity

Internal (bridge-facing, X-Internal-Token):
    GET /api/v1/agent-profiles/internal/{user_id}/{agent_type}
"""

import logging
import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.routes.agent_actions import get_current_user_payload, require_internal_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent-profiles", tags=["agent-profiles"])

AGENT_TYPES = {"autonomous", "customer_service", "leads_qualifier", "finance_invoice_ops", "office_assistant"}

# Per-field length caps: generous enough for a real business identity, small
# enough that a profile can't blow up prompt size or hide a jailbreak essay.
FIELD_CAPS = {
    "agent_name": 80,
    "business_name": 120,
    "tone": 200,
    "language_pref": 60,
    "business_description": 1500,
    "knowledge": 4000,
    "custom_instructions": 1500,
    "greeting": 300,
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS product.agent_profiles (
    user_id              TEXT NOT NULL,
    agent_type           TEXT NOT NULL,
    agent_name           TEXT,
    business_name        TEXT,
    tone                 TEXT,
    language_pref        TEXT,
    business_description TEXT,
    knowledge            TEXT,
    custom_instructions  TEXT,
    greeting             TEXT,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, agent_type)
);
"""

_schema_ready = False

# Strip ASCII control chars except newline/tab (keeps multi-line FAQ readable,
# kills zero-width/escape-sequence smuggling).
_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]")


def _sanitize(value: Optional[str], cap: int) -> Optional[str]:
    if value is None:
        return None
    cleaned = _CONTROL_RE.sub("", str(value)).strip()
    return cleaned[:cap] if cleaned else None


def _connect():
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set — agent profiles require Postgres")
    return psycopg2.connect(dsn, connect_timeout=5)


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_SQL)
    conn.commit()
    _schema_ready = True


_COLUMNS = [
    "agent_name", "business_name", "tone", "language_pref",
    "business_description", "knowledge", "custom_instructions", "greeting",
]


def _row_to_profile(row) -> dict:
    profile = dict(zip(_COLUMNS, row[:-1]))
    profile["updated_at"] = row[-1].isoformat() if row[-1] else None
    return profile


def load_profile(user_id: str, agent_type: str) -> Optional[dict]:
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_COLUMNS)}, updated_at FROM product.agent_profiles"
                " WHERE user_id = %s AND agent_type = %s",
                (user_id, agent_type),
            )
            row = cur.fetchone()
        return _row_to_profile(row) if row else None
    finally:
        conn.close()


class ProfileUpdate(BaseModel):
    agent_name: Optional[str] = Field(default=None, max_length=500)
    business_name: Optional[str] = Field(default=None, max_length=500)
    tone: Optional[str] = Field(default=None, max_length=1000)
    language_pref: Optional[str] = Field(default=None, max_length=500)
    business_description: Optional[str] = Field(default=None, max_length=8000)
    knowledge: Optional[str] = Field(default=None, max_length=20000)
    custom_instructions: Optional[str] = Field(default=None, max_length=8000)
    greeting: Optional[str] = Field(default=None, max_length=1000)


def _check_agent_type(agent_type: str) -> None:
    if agent_type not in AGENT_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown agent type '{agent_type}'")


@router.get("/internal/{user_id}/{agent_type}", dependencies=[Depends(require_internal_token)])
def internal_get(user_id: str, agent_type: str):
    _check_agent_type(agent_type)
    try:
        profile = load_profile(user_id, agent_type)
    except Exception as e:
        logger.error(f"profile lookup failed for {user_id}/{agent_type}: {e}")
        raise HTTPException(status_code=503, detail="Profile store unavailable")
    return {"profile": profile}


@router.get("")
def list_profiles(user: dict = Depends(get_current_user_payload)):
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT agent_type, {', '.join(_COLUMNS)}, updated_at FROM product.agent_profiles"
                " WHERE user_id = %s",
                (user["user_id"],),
            )
            rows = cur.fetchall()
        return {"profiles": {row[0]: _row_to_profile(row[1:]) for row in rows}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"profile list failed for {user['user_id']}: {e}")
        raise HTTPException(status_code=503, detail="Profile store unavailable")
    finally:
        conn.close()


@router.get("/{agent_type}")
def get_profile(agent_type: str, user: dict = Depends(get_current_user_payload)):
    _check_agent_type(agent_type)
    try:
        profile = load_profile(user["user_id"], agent_type)
    except Exception as e:
        logger.error(f"profile lookup failed: {e}")
        raise HTTPException(status_code=503, detail="Profile store unavailable")
    return {"agent_type": agent_type, "profile": profile}


@router.put("/{agent_type}")
def upsert_profile(agent_type: str, body: ProfileUpdate, user: dict = Depends(get_current_user_payload)):
    _check_agent_type(agent_type)
    values = {field: _sanitize(getattr(body, field), cap) for field, cap in FIELD_CAPS.items()}

    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cols = list(values.keys())
            assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
            cur.execute(
                f"""
                INSERT INTO product.agent_profiles (user_id, agent_type, {', '.join(cols)}, updated_at)
                VALUES (%s, %s, {', '.join(['%s'] * len(cols))}, now())
                ON CONFLICT (user_id, agent_type)
                DO UPDATE SET {assignments}, updated_at = now()
                """,
                [user["user_id"], agent_type, *[values[c] for c in cols]],
            )
        conn.commit()
        logger.info(f"Agent profile saved: {agent_type} for {user['user_id']}")
        return {"ok": True, "agent_type": agent_type, "profile": values}
    except Exception as e:
        logger.error(f"profile save failed: {e}")
        raise HTTPException(status_code=503, detail="Profile store unavailable")
    finally:
        conn.close()


@router.delete("/{agent_type}")
def delete_profile(agent_type: str, user: dict = Depends(get_current_user_payload)):
    _check_agent_type(agent_type)
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM product.agent_profiles WHERE user_id = %s AND agent_type = %s",
                (user["user_id"], agent_type),
            )
        conn.commit()
        return {"ok": True}
    except Exception as e:
        logger.error(f"profile delete failed: {e}")
        raise HTTPException(status_code=503, detail="Profile store unavailable")
    finally:
        conn.close()

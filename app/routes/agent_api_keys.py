"""
Tenant-scoped API keys — lets a tenant's own bot/app/backend send messages to
their Aivory agent directly (their own Discord bot, a website chat widget,
an internal tool), instead of being limited to Aivory-hosted Telegram/Slack/
Console. See docs/ADR-006-CERVEAU-CLIENT-DEPLOYMENT-API.md, Part A.

Table: product.agent_api_keys (avry-postgres). Real Postgres, not the
file-JSON DatabaseService convention telegram_bindings/slack_installations
use — a leaked API key is a bearer credential (message-send capability),
unlike a chat binding, so it needs a hashed, uniquely-constrained, atomically
revocable row, the same reasoning agent_profiles/billing.user_credits
already established "real Postgres" for.

Dashboard-facing (JWT auth):
    POST   /api/v1/agent-api-keys                 -> create (plaintext key shown ONCE)
    GET    /api/v1/agent-api-keys?agent_type=...   -> list (never plaintext/hash)
    DELETE /api/v1/agent-api-keys/{id}             -> revoke

Tenant-infra-facing (X-Aivory-Api-Key — a dedicated header, never
Authorization: Bearer, so it can never collide with or be misparsed by the
JWT path):
    POST /api/v1/agent-api/message
"""

import hashlib
import logging
import os
import secrets
from datetime import datetime
from typing import Optional

import requests
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.database.db_service import DatabaseService
from app.routes.agent_actions import get_current_user_payload
from app.services import credit_service
from app.services.telegram_service import AGENT_TYPES, FALLBACK_REPLY, is_superadmin, load_user_record
from app.utils.cache import check_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["agent-api-keys"])

# Module-level, matches app/routes/telegram.py's own convention — not
# constructed per-request.
_db_service = DatabaseService()

# Packaging gate, not a risk-containment one — Part A introduces no new
# execution surface (it's the exact same message/reply loop Telegram/Slack/
# Console already run, just a new transport), so this is purely which plans
# get raw API access as a differentiator.
_TIER_ORDER = {"foundation": 0, "pro": 1, "enterprise": 2}
_MIN_TIER = "pro"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS product.agent_api_keys (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       TEXT NOT NULL,
    agent_type    TEXT NOT NULL,
    key_prefix    TEXT NOT NULL,
    key_hash      TEXT NOT NULL,
    label         TEXT,
    status        TEXT NOT NULL DEFAULT 'active',
    last_used_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ,
    CONSTRAINT agent_api_keys_status_check CHECK (status IN ('active','revoked'))
);
CREATE INDEX IF NOT EXISTS agent_api_keys_user_agent_idx ON product.agent_api_keys (user_id, agent_type);
CREATE UNIQUE INDEX IF NOT EXISTS agent_api_keys_hash_idx ON product.agent_api_keys (key_hash);
"""

_schema_ready = False

# Soft cap per (user_id, agent_type) — a natural upsell lever more than a
# hard technical limit; Enterprise gets a materially higher ceiling.
_MAX_KEYS_PER_AGENT = {"pro": 3, "enterprise": 20}

# Per-key request-rate ceiling — distinct from credit_service's balance gate.
# Credits bound *cost*; this bounds raw request *volume* against a single key
# (a misbehaving integration hammering the endpoint, or a leaked key used for
# abuse before the tenant notices and revokes it). One fixed window per key,
# not per user_id, so a tenant's other keys are unaffected by one noisy one.
_RATE_LIMIT_PER_MINUTE = {"pro": 60, "enterprise": 300}
_RATE_LIMIT_WINDOW_SECONDS = 60


def _connect():
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set — agent API keys require Postgres")
    return psycopg2.connect(dsn, connect_timeout=5)


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_SQL)
    conn.commit()
    _schema_ready = True


def _check_agent_type(agent_type: str) -> None:
    if agent_type not in AGENT_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown agent type '{agent_type}'")


def _require_pro_or_above(user: dict) -> dict:
    if is_superadmin(user):
        return user
    tier = str(user.get("tier") or "foundation").lower()
    if _TIER_ORDER.get(tier, 0) < _TIER_ORDER[_MIN_TIER]:
        raise HTTPException(
            status_code=403,
            detail="API deployment is available on the Pro plan and above. Upgrade to create an API key.",
        )
    return user


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class CreateKeyRequest(BaseModel):
    agent_type: str
    label: Optional[str] = Field(default=None, max_length=200)


@router.post("/agent-api-keys", status_code=201)
def create_key(body: CreateKeyRequest, user: dict = Depends(get_current_user_payload)):
    _check_agent_type(body.agent_type)
    # The JWT payload never carries `tier` (create_access_token only bakes in
    # user_id/email/account_type) — every real tier check must re-load the
    # current record from Postgres, same pattern telegram.py's agent_chat
    # already uses. Checking `user.get("tier")` directly here always reads
    # None and would 403 every real non-superadmin caller regardless of
    # their actual plan.
    record = load_user_record(_db_service, user["user_id"]) or {"user_id": user["user_id"]}
    _require_pro_or_above(record)

    tier = "enterprise" if is_superadmin(record) else str(record.get("tier") or "foundation").lower()
    max_keys = _MAX_KEYS_PER_AGENT.get(tier, _MAX_KEYS_PER_AGENT["pro"])

    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM product.agent_api_keys"
                " WHERE user_id = %s AND agent_type = %s AND status = 'active'",
                (user["user_id"], body.agent_type),
            )
            (active_count,) = cur.fetchone()
            if active_count >= max_keys:
                raise HTTPException(
                    status_code=400,
                    detail=f"Key limit reached ({max_keys} active keys for this agent on your plan). Revoke one first.",
                )

            raw_key = f"avk_live_{secrets.token_urlsafe(32)}"
            key_prefix = raw_key[:12]
            key_hash = _hash_key(raw_key)
            cur.execute(
                """
                INSERT INTO product.agent_api_keys (user_id, agent_type, key_prefix, key_hash, label)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, created_at
                """,
                (user["user_id"], body.agent_type, key_prefix, key_hash, body.label),
            )
            row_id, created_at = cur.fetchone()
        conn.commit()
        logger.info(f"Agent API key created: {body.agent_type} for {user['user_id']} (prefix {key_prefix})")
        return {
            "id": str(row_id),
            "key": raw_key,  # shown exactly once — never retrievable again
            "key_prefix": key_prefix,
            "label": body.label,
            "agent_type": body.agent_type,
            "created_at": created_at.isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"agent API key create failed: {e}")
        raise HTTPException(status_code=503, detail="API key store unavailable")
    finally:
        conn.close()


@router.get("/agent-api-keys")
def list_keys(agent_type: Optional[str] = None, user: dict = Depends(get_current_user_payload)):
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            if agent_type:
                _check_agent_type(agent_type)
                cur.execute(
                    "SELECT id, agent_type, key_prefix, label, status, last_used_at, created_at"
                    " FROM product.agent_api_keys WHERE user_id = %s AND agent_type = %s"
                    " ORDER BY created_at DESC",
                    (user["user_id"], agent_type),
                )
            else:
                cur.execute(
                    "SELECT id, agent_type, key_prefix, label, status, last_used_at, created_at"
                    " FROM product.agent_api_keys WHERE user_id = %s ORDER BY created_at DESC",
                    (user["user_id"],),
                )
            rows = cur.fetchall()
        return {
            "keys": [
                {
                    "id": str(r[0]),
                    "agent_type": r[1],
                    "key_prefix": r[2],
                    "label": r[3],
                    "status": r[4],
                    "last_used_at": r[5].isoformat() if r[5] else None,
                    "created_at": r[6].isoformat(),
                }
                for r in rows
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"agent API key list failed: {e}")
        raise HTTPException(status_code=503, detail="API key store unavailable")
    finally:
        conn.close()


@router.delete("/agent-api-keys/{key_id}")
def revoke_key(key_id: str, user: dict = Depends(get_current_user_payload)):
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE product.agent_api_keys
                SET status = 'revoked', revoked_at = now()
                WHERE id = %s AND user_id = %s AND status = 'active'
                RETURNING id
                """,
                (key_id, user["user_id"]),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            raise HTTPException(status_code=404, detail="Key not found or already revoked")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"agent API key revoke failed: {e}")
        raise HTTPException(status_code=503, detail="API key store unavailable")
    finally:
        conn.close()


# ============================================================================
# TENANT-INFRA-FACING: the actual message relay
# ============================================================================


def _resolve_api_key(raw_key: str) -> Optional[dict]:
    """Returns {id, user_id, agent_type} for a valid, active key, else None.
    Fire-and-forget last_used_at update — never blocks the caller on it."""
    if not raw_key or not raw_key.startswith("avk_live_"):
        return None
    conn = _connect()
    try:
        _ensure_schema(conn)
        key_hash = _hash_key(raw_key)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, user_id, agent_type FROM product.agent_api_keys"
                " WHERE key_hash = %s AND status = 'active'",
                (key_hash,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                "UPDATE product.agent_api_keys SET last_used_at = now() WHERE id = %s",
                (row[0],),
            )
        conn.commit()
        return {"id": str(row[0]), "user_id": row[1], "agent_type": row[2]}
    except Exception as e:
        logger.error(f"agent API key resolve failed: {e}")
        return None
    finally:
        conn.close()


class ApiMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    session_id: Optional[str] = Field(default=None, max_length=200)


@router.post("/agent-api/message")
def api_message(
    body: ApiMessageRequest,
    x_aivory_api_key: str = Header(default=""),
):
    key_info = _resolve_api_key(x_aivory_api_key)
    if not key_info:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    user_id, agent_type = key_info["user_id"], key_info["agent_type"]

    # Re-check tier at message time, not just key-creation time — a tier
    # downgrade after a key was minted must not leave a working key forever.
    user = load_user_record(_db_service, user_id)
    if not user or not _tier_ok(user):
        raise HTTPException(
            status_code=403,
            detail="API deployment requires the Pro plan or above; this account no longer qualifies.",
        )

    tier = "enterprise" if is_superadmin(user) else str(user.get("tier") or "foundation").lower()
    rate_limit = _RATE_LIMIT_PER_MINUTE.get(tier, _RATE_LIMIT_PER_MINUTE["pro"])
    if not is_superadmin(user):
        try:
            within_limit = check_rate_limit(
                f"agent_api_key:{key_info['id']}", limit=rate_limit, window=_RATE_LIMIT_WINDOW_SECONDS
            )
        except Exception as e:
            logger.error(f"rate limit check failed for key {key_info['id']}: {e}")
            within_limit = True  # fail open — Redis being down must not block a paying tenant's traffic
        if not within_limit:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded ({rate_limit} requests/minute for this key). Try again shortly.",
            )

    # Fast-fail credit pre-check — cheaper than round-tripping to the bridge
    # for a 200-with-a-sorry-message the way a chat channel gets. The
    # bridge's own row-locked consume() is still the authoritative,
    # race-safe enforcement; this is a fast path on top, same relationship
    # Part B's credit pre-checks have to the ledger.
    try:
        status = credit_service.get_status(user_id)
        if not status.get("unlimited") and (status.get("balance") or 0) <= 0:
            raise HTTPException(
                status_code=402,
                detail={"error": "insufficient_credits", "balance": status.get("balance", 0)},
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"credit pre-check failed for {user_id}: {e}")
        # fail open on the pre-check itself — the bridge's own consume() call
        # right after is still the real enforcement

    session_id = body.session_id or "default"
    if not settings.telegram_agent_gateway_url:
        return {"reply": FALLBACK_REPLY, "session_id": session_id}

    headers = {}
    gateway_token = os.getenv("TELEGRAM_GATEWAY_TOKEN")
    if gateway_token:
        headers["X-Internal-Token"] = gateway_token
    try:
        resp = requests.post(
            f"{settings.telegram_agent_gateway_url.rstrip('/')}/telegram/message",
            headers=headers,
            json={
                "user_id": user_id,
                "agent_type": agent_type,
                "account_type": user.get("account_type", "free"),
                "chat_id": 0,
                "session_id": f"api_{user_id}_{agent_type}_{session_id}",
                "text": body.text,
                "channel": "api",
            },
            timeout=195,
        )
    except requests.RequestException as e:
        logger.error(f"agent-api gateway error for {user_id}/{agent_type}: {e}")
        raise HTTPException(status_code=502, detail="Agent gateway unreachable")

    if resp.status_code == 402:
        raise HTTPException(status_code=402, detail="insufficient_credits")
    if not resp.ok:
        logger.error(f"agent-api gateway returned {resp.status_code}: {resp.text[:200]}")
        raise HTTPException(status_code=504 if resp.status_code >= 500 else 502, detail="Agent gateway error")

    data = resp.json()
    reply = (data.get("reply") or FALLBACK_REPLY)[:4096]
    return {"reply": reply, "session_id": session_id}


def _tier_ok(user: dict) -> bool:
    if is_superadmin(user):
        return True
    tier = str(user.get("tier") or "foundation").lower()
    return _TIER_ORDER.get(tier, 0) >= _TIER_ORDER[_MIN_TIER]

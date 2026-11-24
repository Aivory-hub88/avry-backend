"""
BYOK (bring your own key) — a user stores their own LLM provider key so model
calls made on their behalf are billed to their provider account, not Aivory's.

Table: product.byok_keys (avry-postgres), one row per (user_id, provider).
The key is AES-256-GCM encrypted at rest (app/services/mcp_server_encryption.py,
MCP_SERVER_AUTH_ENCRYPTION_KEY) and NEVER returned to the browser — the
dashboard only ever sees provider, a masked hint (last 4 chars), and status.

Dashboard-facing (JWT auth):
    GET    /api/v1/byok-keys                    -> list (masked)
    PUT    /api/v1/byok-keys/{provider}         -> save/replace (validated live first)
    POST   /api/v1/byok-keys/{provider}/test    -> re-check the stored key
    DELETE /api/v1/byok-keys/{provider}         -> remove

Service-facing (X-Internal-Token, same gate as agent_actions):
    GET /api/v1/byok-keys/internal/{user_id}/{provider} -> {"api_key": "..."} | 404
    Used by the bridge / LLM clients to resolve a user's key at call time.
"""

import logging
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.routes.agent_actions import get_current_user_payload, require_internal_token
from app.services.mcp_server_encryption import decrypt_auth_header_value, encrypt_auth_header_value

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/byok-keys", tags=["byok"])

PROVIDERS = ("openrouter", "anthropic", "openai")

_KEY_PREFIXES = {
    "openrouter": "sk-or-",
    "anthropic": "sk-ant-",
    "openai": "sk-",
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS product.byok_keys (
    user_id           TEXT NOT NULL,
    provider          TEXT NOT NULL,
    api_key_encrypted BYTEA NOT NULL,
    key_hint          TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'active',
    last_verified_at  TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, provider),
    CONSTRAINT byok_keys_status_check CHECK (status IN ('active','invalid'))
);
"""

_schema_ready = False


def _connect():
    import os
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set — BYOK keys require Postgres")
    return psycopg2.connect(dsn, connect_timeout=5)


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_SQL)
    conn.commit()
    _schema_ready = True


def _check_provider(provider: str) -> None:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider '{provider}'")


def _hint(api_key: str) -> str:
    return "…" + api_key[-4:]


def verify_provider_key(provider: str, api_key: str) -> tuple[bool, Optional[str]]:
    """Cheap authenticated GET against the provider. Returns (ok, reason).
    A network failure is reported as not-ok with a reason, never raised."""
    try:
        if provider == "openrouter":
            r = requests.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
        elif provider == "anthropic":
            r = requests.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                timeout=10,
            )
        else:
            r = requests.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
    except requests.RequestException as e:
        logger.warning(f"BYOK verify {provider} network error: {e}")
        return False, "Could not reach the provider to verify the key"
    if r.status_code == 200:
        return True, None
    if r.status_code in (401, 403):
        return False, "The provider rejected this key"
    return False, f"The provider returned {r.status_code}"


class SaveKeyRequest(BaseModel):
    api_key: str = Field(min_length=20, max_length=512)


def _row_to_dict(provider, hint, status, last_verified_at, updated_at) -> dict:
    return {
        "provider": provider,
        "key_hint": hint,
        "status": status,
        "last_verified_at": last_verified_at.isoformat() if last_verified_at else None,
        "updated_at": updated_at.isoformat(),
    }


@router.get("")
def list_keys(user: dict = Depends(get_current_user_payload)):
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT provider, key_hint, status, last_verified_at, updated_at"
                " FROM product.byok_keys WHERE user_id = %s ORDER BY provider",
                (user["user_id"],),
            )
            rows = cur.fetchall()
        return {"keys": [_row_to_dict(*r) for r in rows]}
    except Exception as e:
        logger.error(f"byok list failed: {e}")
        raise HTTPException(status_code=503, detail="Key store unavailable")
    finally:
        conn.close()


@router.put("/{provider}")
def save_key(provider: str, body: SaveKeyRequest, user: dict = Depends(get_current_user_payload)):
    _check_provider(provider)
    api_key = body.api_key.strip()
    if not api_key.startswith(_KEY_PREFIXES[provider]):
        raise HTTPException(
            status_code=400,
            detail=f"That doesn't look like a {provider} key (expected it to start with '{_KEY_PREFIXES[provider]}')",
        )
    ok, reason = verify_provider_key(provider, api_key)
    if not ok:
        raise HTTPException(status_code=400, detail=reason or "Key verification failed")

    conn = _connect()
    try:
        _ensure_schema(conn)
        encrypted = encrypt_auth_header_value(api_key)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO product.byok_keys (user_id, provider, api_key_encrypted, key_hint, status, last_verified_at)
                VALUES (%s, %s, %s, %s, 'active', now())
                ON CONFLICT (user_id, provider) DO UPDATE SET
                    api_key_encrypted = EXCLUDED.api_key_encrypted,
                    key_hint = EXCLUDED.key_hint,
                    status = 'active',
                    last_verified_at = now(),
                    updated_at = now()
                RETURNING provider, key_hint, status, last_verified_at, updated_at
                """,
                (user["user_id"], provider, encrypted, _hint(api_key)),
            )
            row = cur.fetchone()
        conn.commit()
        logger.info(f"BYOK key saved: {provider} for {user['user_id']}")
        return _row_to_dict(*row)
    except ValueError as e:
        logger.error(f"byok encryption unavailable: {e}")
        raise HTTPException(status_code=503, detail="Key store unavailable")
    except Exception as e:
        logger.error(f"byok save failed: {e}")
        raise HTTPException(status_code=503, detail="Key store unavailable")
    finally:
        conn.close()


def _load_plaintext(user_id: str, provider: str) -> Optional[str]:
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT api_key_encrypted FROM product.byok_keys"
                " WHERE user_id = %s AND provider = %s AND status = 'active'",
                (user_id, provider),
            )
            row = cur.fetchone()
        if not row:
            return None
        return decrypt_auth_header_value(bytes(row[0]))
    finally:
        conn.close()


@router.post("/{provider}/test")
def test_key(provider: str, user: dict = Depends(get_current_user_payload)):
    _check_provider(provider)
    try:
        api_key = _load_plaintext(user["user_id"], provider)
    except Exception as e:
        logger.error(f"byok test load failed: {e}")
        raise HTTPException(status_code=503, detail="Key store unavailable")
    if not api_key:
        raise HTTPException(status_code=404, detail="No key saved for this provider")

    ok, reason = verify_provider_key(provider, api_key)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE product.byok_keys SET status = %s,"
                " last_verified_at = CASE WHEN %s THEN now() ELSE last_verified_at END"
                " WHERE user_id = %s AND provider = %s",
                ("active" if ok else "invalid", ok, user["user_id"], provider),
            )
        conn.commit()
    finally:
        conn.close()
    return {"ok": ok, "reason": reason}


@router.delete("/{provider}")
def delete_key(provider: str, user: dict = Depends(get_current_user_payload)):
    _check_provider(provider)
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM product.byok_keys WHERE user_id = %s AND provider = %s RETURNING provider",
                (user["user_id"], provider),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            raise HTTPException(status_code=404, detail="No key saved for this provider")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"byok delete failed: {e}")
        raise HTTPException(status_code=503, detail="Key store unavailable")
    finally:
        conn.close()


def get_user_byok_key(user_id: str, provider: str) -> Optional[str]:
    """In-process resolver for backend LLM clients. None when the user has no
    active key (or the store is unreachable) — callers fall back to Aivory's
    own key, and must then bill credits as usual."""
    if provider not in PROVIDERS:
        return None
    try:
        return _load_plaintext(user_id, provider)
    except Exception as e:
        logger.error(f"byok resolve failed for {user_id}/{provider}: {e}")
        return None


@router.get("/internal/{user_id}/{provider}", dependencies=[Depends(require_internal_token)])
def resolve_key_internal(user_id: str, provider: str):
    _check_provider(provider)
    api_key = get_user_byok_key(user_id, provider)
    if not api_key:
        raise HTTPException(status_code=404, detail="No active key")
    return {"api_key": api_key}

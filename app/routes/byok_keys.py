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
    PUT    /api/v1/byok-keys/settings           -> master switch + primary provider

Service-facing (X-Internal-Token, same gate as agent_actions):
    GET /api/v1/byok-keys/internal/{user_id}            -> {"provider", "api_key"} | 404
        The key a turn should run on, with the user's switch and primary
        provider applied. 404 means "use Aivory's key and charge credits".
    GET /api/v1/byok-keys/internal/{user_id}/{provider} -> {"api_key": "..."} | 404
        One provider's key; 404 too while the user has BYOK switched off.
"""

import logging
import re
from urllib.parse import urlsplit
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.services.guarded_fetch import GuardedFetchError, guarded_fetch
from app.routes.agent_actions import get_current_user_payload, require_internal_token
from app.services.mcp_server_encryption import decrypt_auth_header_value, encrypt_auth_header_value

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/byok-keys", tags=["byok"])

# `openai_compatible` is any OpenAI-style /chat/completions endpoint the user
# points us at (Together, Groq, a vLLM gateway…). It is the only provider whose
# URL is user-supplied, so it is the only one that goes through the SSRF guard.
PROVIDERS = ("openrouter", "anthropic", "openai", "openai_compatible")
COMPATIBLE = "openai_compatible"

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
ALTER TABLE product.byok_keys ADD COLUMN IF NOT EXISTS base_url TEXT;
ALTER TABLE product.byok_keys ADD COLUMN IF NOT EXISTS model TEXT;
CREATE TABLE IF NOT EXISTS product.byok_settings (
    user_id            TEXT PRIMARY KEY,
    enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    preferred_provider TEXT,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
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


def normalize_base_url(raw: str) -> str:
    """The OpenAI-compatible base URL, e.g. https://api.together.xyz/v1.
    https only, no credentials/query/fragment in it. Whether the host is a safe
    (public) one is decided where it is resolved, by the SSRF guard, not here."""
    url = (raw or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise HTTPException(status_code=400, detail="The base URL must start with https://")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise HTTPException(status_code=400, detail="The base URL can't contain credentials, a query or a fragment")
    if len(url) > 300:
        raise HTTPException(status_code=400, detail="The base URL is too long")
    return url


_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")


def normalize_model(raw: Optional[str]) -> str:
    model = (raw or "").strip()
    if not _MODEL_RE.match(model):
        raise HTTPException(status_code=400, detail="Enter the model name to use, e.g. meta-llama/Llama-3.3-70B-Instruct")
    return model


def verify_compatible(base_url: str, api_key: str) -> tuple[bool, Optional[str], Optional[str]]:
    """(ok, reason, note) for an OpenAI-compatible endpoint, via the SSRF guard:
    it refuses loopback / private / metadata addresses and never follows a
    redirect into one. A server with no /models endpoint can't be verified, so
    that is accepted with a note instead of being refused."""
    try:
        r = guarded_fetch(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
    except GuardedFetchError as e:
        return False, str(e), None
    if r.status == 200:
        return True, None, None
    if r.status in (401, 403):
        return False, "The server rejected this key", None
    if r.status in (404, 405, 501):
        return True, None, "Saved, but this server has no /models endpoint, so the key could not be verified."
    return False, f"The server returned {r.status}", None


class SaveKeyRequest(BaseModel):
    api_key: str = Field(min_length=8, max_length=512)
    # Only for openai_compatible.
    base_url: Optional[str] = Field(default=None, max_length=400)
    model: Optional[str] = Field(default=None, max_length=200)


def _row_to_dict(provider, hint, status, last_verified_at, updated_at, base_url=None, model=None) -> dict:
    return {
        "base_url": base_url,
        "model": model,
        "provider": provider,
        "key_hint": hint,
        "status": status,
        "last_verified_at": last_verified_at.isoformat() if last_verified_at else None,
        "updated_at": updated_at.isoformat(),
    }


def _load_settings(cur, user_id: str) -> dict:
    """The user's switch and primary provider. No row means the defaults:
    on, with no preferred provider — a saved key works without any setup."""
    cur.execute(
        "SELECT enabled, preferred_provider FROM product.byok_settings WHERE user_id = %s",
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return {"enabled": True, "preferred_provider": None}
    return {"enabled": bool(row[0]), "preferred_provider": row[1]}


@router.get("")
def list_keys(user: dict = Depends(get_current_user_payload)):
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT provider, key_hint, status, last_verified_at, updated_at, base_url, model"
                " FROM product.byok_keys WHERE user_id = %s ORDER BY provider",
                (user["user_id"],),
            )
            rows = cur.fetchall()
            settings = _load_settings(cur, user["user_id"])
        return {"keys": [_row_to_dict(*r) for r in rows], "settings": settings}
    except Exception as e:
        logger.error(f"byok list failed: {e}")
        raise HTTPException(status_code=503, detail="Key store unavailable")
    finally:
        conn.close()


class SettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    # Explicit null clears the choice (back to automatic).
    preferred_provider: Optional[str] = None


# Declared before PUT /{provider}: otherwise "settings" would match as a provider.
@router.put("/settings")
def update_settings(body: SettingsRequest, user: dict = Depends(get_current_user_payload)):
    dump = getattr(body, "model_dump", None) or body.dict  # pydantic v2 / v1
    changes = dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="Nothing to update")
    if changes.get("preferred_provider") is not None:
        _check_provider(changes["preferred_provider"])

    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            current = _load_settings(cur, user["user_id"])
            merged = {**current, **changes}
            if merged["enabled"] is None:
                merged["enabled"] = current["enabled"]
            preferred = merged["preferred_provider"]
            if preferred is not None:
                cur.execute(
                    "SELECT 1 FROM product.byok_keys WHERE user_id = %s AND provider = %s",
                    (user["user_id"], preferred),
                )
                if not cur.fetchone():
                    raise HTTPException(status_code=400, detail=f"Save a {preferred} key first")
            cur.execute(
                """
                INSERT INTO product.byok_settings (user_id, enabled, preferred_provider)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    preferred_provider = EXCLUDED.preferred_provider,
                    updated_at = now()
                """,
                (user["user_id"], merged["enabled"], preferred),
            )
        conn.commit()
        logger.info(f"BYOK settings updated for {user['user_id']}: {merged}")
        return {"enabled": merged["enabled"], "preferred_provider": preferred}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"byok settings update failed: {e}")
        raise HTTPException(status_code=503, detail="Key store unavailable")
    finally:
        conn.close()


@router.put("/{provider}")
def save_key(provider: str, body: SaveKeyRequest, user: dict = Depends(get_current_user_payload)):
    _check_provider(provider)
    api_key = body.api_key.strip()
    base_url = model = note = None
    if provider == COMPATIBLE:
        base_url = normalize_base_url(body.base_url or "")
        model = normalize_model(body.model)
        ok, reason, note = verify_compatible(base_url, api_key)
    else:
        if len(api_key) < 20:
            raise HTTPException(status_code=400, detail="That key looks too short")
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
                INSERT INTO product.byok_keys (user_id, provider, api_key_encrypted, key_hint, status, last_verified_at, base_url, model)
                VALUES (%s, %s, %s, %s, 'active', now(), %s, %s)
                ON CONFLICT (user_id, provider) DO UPDATE SET
                    api_key_encrypted = EXCLUDED.api_key_encrypted,
                    key_hint = EXCLUDED.key_hint,
                    status = 'active',
                    last_verified_at = now(),
                    base_url = EXCLUDED.base_url,
                    model = EXCLUDED.model,
                    updated_at = now()
                RETURNING provider, key_hint, status, last_verified_at, updated_at, base_url, model
                """,
                (user["user_id"], provider, encrypted, _hint(api_key), base_url, model),
            )
            row = cur.fetchone()
        conn.commit()
        logger.info(f"BYOK key saved: {provider} for {user['user_id']}")
        result = _row_to_dict(*row)
        if note:
            result["note"] = note
        return result
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


def _load_endpoint(user_id: str, provider: str) -> tuple[Optional[str], Optional[str]]:
    """(base_url, model) of a saved key; both None for the fixed providers."""
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT base_url, model FROM product.byok_keys WHERE user_id = %s AND provider = %s",
                (user_id, provider),
            )
            row = cur.fetchone()
        return (row[0], row[1]) if row else (None, None)
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

    if provider == COMPATIBLE:
        base_url, _model = _load_endpoint(user["user_id"], provider)
        if not base_url:
            raise HTTPException(status_code=404, detail="No endpoint saved for this key")
        ok, reason, _note = verify_compatible(base_url, api_key)
    else:
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
        # A removed key can't stay the primary provider.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE product.byok_settings SET preferred_provider = NULL, updated_at = now()"
                " WHERE user_id = %s AND preferred_provider = %s",
                (user["user_id"], provider),
            )
        conn.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"byok delete failed: {e}")
        raise HTTPException(status_code=503, detail="Key store unavailable")
    finally:
        conn.close()


def _is_enabled(user_id: str) -> bool:
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            return _load_settings(cur, user_id)["enabled"]
    finally:
        conn.close()


def get_user_byok_key(user_id: str, provider: str) -> Optional[str]:
    """In-process resolver for backend LLM clients. None when the user has no
    active key, has switched BYOK off, or the store is unreachable — callers
    fall back to Aivory's own key, and must then bill credits as usual."""
    if provider not in PROVIDERS:
        return None
    try:
        if not _is_enabled(user_id):
            return None
        return _load_plaintext(user_id, provider)
    except Exception as e:
        logger.error(f"byok resolve failed for {user_id}/{provider}: {e}")
        return None


def resolve_active_byok(user_id: str) -> Optional[tuple[str, str]]:
    """(provider, api_key) a turn should run on: nothing if the user switched
    BYOK off, otherwise their primary provider if it still has an active key,
    else the first provider with one."""
    try:
        conn = _connect()
        try:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                settings = _load_settings(cur, user_id)
        finally:
            conn.close()
        if not settings["enabled"]:
            return None
        order = [p for p in [settings["preferred_provider"], *PROVIDERS] if p]
        for provider in dict.fromkeys(order):
            api_key = _load_plaintext(user_id, provider)
            if api_key:
                return provider, api_key
        return None
    except Exception as e:
        logger.error(f"byok resolve failed for {user_id}: {e}")
        return None


# Declared before the two-segment route below so neither can shadow the other.
@router.get("/internal/{user_id}", dependencies=[Depends(require_internal_token)])
def resolve_active_internal(user_id: str):
    active = resolve_active_byok(user_id)
    if not active:
        raise HTTPException(status_code=404, detail="No active key")
    result = {"provider": active[0], "api_key": active[1]}
    if active[0] == COMPATIBLE:
        base_url, model = _load_endpoint(user_id, active[0])
        if not base_url or not model:
            raise HTTPException(status_code=404, detail="No active key")
        result.update({"base_url": base_url, "model": model})
    return result


@router.get("/internal/{user_id}/{provider}", dependencies=[Depends(require_internal_token)])
def resolve_key_internal(user_id: str, provider: str):
    _check_provider(provider)
    api_key = get_user_byok_key(user_id, provider)
    if not api_key:
        raise HTTPException(status_code=404, detail="No active key")
    return {"api_key": api_key}

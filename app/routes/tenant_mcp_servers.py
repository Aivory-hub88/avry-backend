"""
Tenant custom MCP servers — lets a Pro/Enterprise operator register their own
MCP server (a thin shim over their internal systems) so their Cerveau agent
can read/act on their own environment, instead of being limited to the
toolkits Aivory curates. See docs/ADR-006-CERVEAU-CLIENT-DEPLOYMENT-API.md,
Part B.

Table: product.tenant_custom_mcp_servers (avry-postgres). auth header values
are encrypted at rest (AES-256-GCM, app/services/mcp_server_encryption.py)
and never returned by any dashboard-facing route.

Every network call this module makes to a tenant-supplied URL goes through
app/services/guarded_fetch.py — SSRF-guarded (https-only, DNS-pinned,
deny-listed, size-capped, no auto-redirect). This is the single most
dangerous surface in the codebase: a tenant-registered URL of
`https://internal-lookalike.example/` that resolves to `127.0.0.1` would let
"MCP server verification" probe Cerveau's own webhook from inside Aivory's
own trust boundary if this guard were ever bypassed. See guarded_fetch.py's
own module docstring for the full control list.

Dashboard-facing (JWT auth):
    POST   /api/v1/tenant-mcp-servers            -> register + synchronously verify
    GET    /api/v1/tenant-mcp-servers?agent_type=... -> list (never returns the auth header value)
    POST   /api/v1/tenant-mcp-servers/{id}/reverify  -> re-run verification
    DELETE /api/v1/tenant-mcp-servers/{id}        -> disable

Internal (Cerveau-facing, X-Internal-Token):
    GET /api/v1/tenant-mcp-servers/internal/{user_id}/{agent_type}
        -> decrypted, status='verified' rows only

Tier gate: Pro and Enterprise (§B6, revised 2026-08-15 — the original
Enterprise-only recommendation was a starting-population choice, not a
technical requirement; B1-B5's guarded-fetcher now has a real, live-verified
production track record, so the user opted to open it to Pro immediately
rather than wait for a Phase 2). Engine gate: engine='cerveau' only, hard
requirement (§B7) — the legacy Node loop has no risk-tier/approval-gate
concept at all, so shipping arbitrary tenant-supplied tool execution against
it would mean zero safety net.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.database.db_service import DatabaseService
from app.routes.agent_actions import get_current_user_payload, require_internal_token
from app.routes.agent_profiles import AGENT_TYPES, load_profile_internal
from app.services import mcp_server_encryption
from app.services.guarded_fetch import GuardedFetchError, guarded_fetch
from app.services.telegram_service import is_superadmin, load_user_record

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tenant-mcp-servers", tags=["tenant-mcp-servers"])

# Module-level, matches app/routes/telegram.py / agent_api_keys.py's own convention.
_db_service = DatabaseService()

_MIN_TIER = "pro"
_TIER_ORDER = {"foundation": 0, "pro": 1, "enterprise": 2}

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")
_MAX_ERROR_LEN = 500

# v1 caps at one row per (user_id, agent_type) — schema supports more later
# without a migration, but route logic enforces this today.
_MAX_SERVERS_PER_AGENT = 1

_VERIFY_CONNECT_TIMEOUT = 3.0
_VERIFY_TOTAL_TIMEOUT = 10.0
_MCP_PROTOCOL_VERSION = "2024-11-05"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS product.tenant_custom_mcp_servers (
    id                            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                       TEXT NOT NULL,
    agent_type                    TEXT NOT NULL,
    name                          TEXT NOT NULL,
    url                           TEXT NOT NULL,
    transport                     TEXT NOT NULL DEFAULT 'streamable-http',
    auth_header_name              TEXT,
    auth_header_value_encrypted   BYTEA,
    status                        TEXT NOT NULL DEFAULT 'pending_verification',
    risk_tier                     TEXT NOT NULL DEFAULT 'irreversible',
    last_verified_at              TIMESTAMPTZ,
    last_verify_error             TEXT,
    tool_count                    INTEGER,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_at                   TIMESTAMPTZ,
    CONSTRAINT tenant_custom_mcp_servers_status_check
        CHECK (status IN ('pending_verification','verified','verification_failed','disabled')),
    CONSTRAINT tenant_custom_mcp_servers_transport_check
        CHECK (transport IN ('streamable-http','sse')),
    CONSTRAINT tenant_custom_mcp_servers_risk_tier_check
        CHECK (risk_tier IN ('safe','reversible','irreversible'))
);
CREATE UNIQUE INDEX IF NOT EXISTS tenant_custom_mcp_servers_user_agent_name_idx
    ON product.tenant_custom_mcp_servers (user_id, agent_type, name);
"""

_schema_ready = False


def _connect():
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set — tenant MCP servers require Postgres")
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


def _require_pro_or_above(user_id: str) -> None:
    # The JWT payload never carries `tier` (create_access_token only bakes in
    # user_id/email/account_type) — every real tier check must re-load the
    # current record from Postgres, same pattern telegram.py's agent_chat
    # already uses. Checking a claim off the raw JWT here would 403 every
    # real non-superadmin caller regardless of their actual plan.
    record = load_user_record(_db_service, user_id) or {"user_id": user_id}
    if is_superadmin(record):
        return
    tier = str(record.get("tier") or "foundation").lower()
    if _TIER_ORDER.get(tier, 0) < _TIER_ORDER[_MIN_TIER]:
        raise HTTPException(
            status_code=403,
            detail="Custom MCP servers are available on the Pro plan and above. Upgrade to register one.",
        )


def _require_cerveau_engine(user_id: str, agent_type: str) -> None:
    profile = load_profile_internal(user_id, agent_type)
    engine = (profile or {}).get("engine") or "legacy"
    if engine != "cerveau":
        raise HTTPException(
            status_code=403,
            detail="Custom MCP servers require this agent to be running on Aivory Cerveau.",
        )


def _validate_https_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise HTTPException(status_code=400, detail="url must be https://")
    if not parts.hostname:
        raise HTTPException(status_code=400, detail="url has no hostname")
    return url


# ── MCP JSON-RPC verification handshake ────────────────────────────────────


def _extract_jsonrpc_body(raw: bytes) -> dict:
    """A streamable-http/SSE MCP response may be plain JSON or SSE-framed
    (`data: {...}` lines) — same ambiguity Cerveau's own runtime transport
    (mcp_transport.rs) already has to handle for the exact same protocol."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise ValueError("empty response body")
    if text.startswith("data:") or "\ndata:" in text:
        lines = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        text = "\n".join(lines).strip()
    return json.loads(text)


def _mcp_jsonrpc_call(url: str, method: str, params: dict, headers: dict, request_id: int) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode("utf-8")
    resp = guarded_fetch(
        url,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **headers,
        },
        body=body,
        connect_timeout=_VERIFY_CONNECT_TIMEOUT,
        total_timeout=_VERIFY_TOTAL_TIMEOUT,
    )
    if resp.status >= 400:
        raise GuardedFetchError(f"server returned HTTP {resp.status}")
    parsed = _extract_jsonrpc_body(resp.body)
    if "error" in parsed and parsed["error"]:
        err = parsed["error"]
        raise GuardedFetchError(f"MCP error: {err.get('message') or err}")
    return parsed.get("result") or {}


def _run_verification(url: str, auth_header_name: Optional[str], auth_header_value: Optional[str]) -> dict:
    """Real MCP initialize + tools/list handshake through the guarded
    fetcher. Returns {tools: [{name, description}]} on success. Raises
    GuardedFetchError (safe-to-display reason) on any failure — SSRF
    rejection, network failure, non-2xx, or malformed/error JSON-RPC."""
    headers = {}
    if auth_header_name and auth_header_value:
        headers[auth_header_name] = auth_header_value

    _run_verification_init(url, headers)
    result = _mcp_jsonrpc_call(url, "tools/list", {}, headers, request_id=2)
    raw_tools = result.get("tools")
    if not isinstance(raw_tools, list):
        raise GuardedFetchError("server did not return a tools list")

    tools = []
    for t in raw_tools:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        tools.append({"name": str(t["name"])[:200], "description": str(t.get("description") or "")[:500]})
    return {"tools": tools}


def _run_verification_init(url: str, headers: dict) -> None:
    _mcp_jsonrpc_call(
        url,
        "initialize",
        {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "aivory-cerveau-verify", "version": "1"},
        },
        headers,
        request_id=1,
    )


# ── Request/response models ─────────────────────────────────────────────


class RegisterServerRequest(BaseModel):
    agent_type: str
    name: str = Field(min_length=1, max_length=40)
    url: str = Field(min_length=1, max_length=2000)
    transport: str = Field(default="streamable-http")
    auth_header_name: Optional[str] = Field(default=None, max_length=200)
    auth_header_value: Optional[str] = Field(default=None, max_length=4000)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("name must match ^[a-zA-Z0-9_-]{1,40}$")
        return v

    @field_validator("transport")
    @classmethod
    def _validate_transport(cls, v: str) -> str:
        if v not in ("streamable-http", "sse"):
            raise ValueError("transport must be 'streamable-http' or 'sse'")
        return v


def _row_to_public_dict(row) -> dict:
    (
        row_id,
        agent_type,
        name,
        url,
        transport,
        auth_header_name,
        status,
        last_verified_at,
        last_verify_error,
        tool_count,
        created_at,
    ) = row
    return {
        "id": str(row_id),
        "agent_type": agent_type,
        "name": name,
        "url": url,
        "transport": transport,
        "auth_header_name": auth_header_name,
        "status": status,
        "last_verified_at": last_verified_at.isoformat() if last_verified_at else None,
        "last_verify_error": last_verify_error,
        "tool_count": tool_count,
        "created_at": created_at.isoformat(),
    }


@router.post("", status_code=201)
def register_server(body: RegisterServerRequest, user: dict = Depends(get_current_user_payload)):
    _check_agent_type(body.agent_type)
    _require_pro_or_above(user["user_id"])
    _require_cerveau_engine(user["user_id"], body.agent_type)
    _validate_https_url(body.url)

    encrypted_auth = None
    if body.auth_header_value:
        if not body.auth_header_name:
            raise HTTPException(status_code=400, detail="auth_header_name is required when auth_header_value is set")
        encrypted_auth = mcp_server_encryption.encrypt_auth_header_value(body.auth_header_value)

    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM product.tenant_custom_mcp_servers"
                " WHERE user_id = %s AND agent_type = %s AND status != 'disabled'",
                (user["user_id"], body.agent_type),
            )
            (active_count,) = cur.fetchone()
            if active_count >= _MAX_SERVERS_PER_AGENT:
                raise HTTPException(
                    status_code=400,
                    detail=f"Only {_MAX_SERVERS_PER_AGENT} custom MCP server is allowed per agent today. Remove the existing one first.",
                )

            cur.execute(
                """
                INSERT INTO product.tenant_custom_mcp_servers
                    (user_id, agent_type, name, url, transport, auth_header_name, auth_header_value_encrypted)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    user["user_id"],
                    body.agent_type,
                    body.name,
                    body.url,
                    body.transport,
                    body.auth_header_name,
                    encrypted_auth,
                ),
            )
            (row_id,) = cur.fetchone()
        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"tenant MCP server insert failed for {user['user_id']}: {e}")
        raise HTTPException(status_code=503, detail="Tenant MCP server store unavailable")
    finally:
        conn.close()

    return _verify_and_persist(str(row_id), user["user_id"], body.agent_type, body.url, body.auth_header_name, body.auth_header_value)


def _verify_and_persist(
    row_id: str,
    user_id: str,
    agent_type: str,
    url: str,
    auth_header_name: Optional[str],
    auth_header_value: Optional[str],
) -> dict:
    tools: list = []
    try:
        result = _run_verification(url, auth_header_name, auth_header_value)
        tools = result["tools"]
        status = "verified"
        error = None
    except GuardedFetchError as e:
        status = "verification_failed"
        error = str(e)[:_MAX_ERROR_LEN]
    except Exception as e:
        logger.error(f"tenant MCP server verify crashed for {user_id}/{agent_type} ({row_id}): {e}")
        status = "verification_failed"
        error = "verification failed unexpectedly"

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE product.tenant_custom_mcp_servers
                SET status = %s, last_verified_at = %s, last_verify_error = %s,
                    tool_count = %s, updated_at = now()
                WHERE id = %s
                RETURNING id, agent_type, name, url, transport, auth_header_name,
                          status, last_verified_at, last_verify_error, tool_count, created_at
                """,
                (
                    status,
                    datetime.now(timezone.utc) if status == "verified" else None,
                    error,
                    len(tools) if status == "verified" else None,
                    row_id,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception as e:
        logger.error(f"tenant MCP server verify-persist failed for {row_id}: {e}")
        raise HTTPException(status_code=503, detail="Tenant MCP server store unavailable")
    finally:
        conn.close()

    payload = _row_to_public_dict(row)
    if status == "verified":
        payload["tools"] = tools
        return payload
    raise HTTPException(status_code=422, detail={"error": "verification_failed", "reason": error, "server": payload})


@router.get("")
def list_servers(agent_type: Optional[str] = None, user: dict = Depends(get_current_user_payload)):
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            if agent_type:
                _check_agent_type(agent_type)
                cur.execute(
                    "SELECT id, agent_type, name, url, transport, auth_header_name, status,"
                    " last_verified_at, last_verify_error, tool_count, created_at"
                    " FROM product.tenant_custom_mcp_servers"
                    " WHERE user_id = %s AND agent_type = %s AND status != 'disabled'"
                    " ORDER BY created_at DESC",
                    (user["user_id"], agent_type),
                )
            else:
                cur.execute(
                    "SELECT id, agent_type, name, url, transport, auth_header_name, status,"
                    " last_verified_at, last_verify_error, tool_count, created_at"
                    " FROM product.tenant_custom_mcp_servers"
                    " WHERE user_id = %s AND status != 'disabled'"
                    " ORDER BY created_at DESC",
                    (user["user_id"],),
                )
            rows = cur.fetchall()
        return {"servers": [_row_to_public_dict(r) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"tenant MCP server list failed for {user['user_id']}: {e}")
        raise HTTPException(status_code=503, detail="Tenant MCP server store unavailable")
    finally:
        conn.close()


@router.post("/{server_id}/reverify")
def reverify_server(server_id: str, user: dict = Depends(get_current_user_payload)):
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT agent_type, url, auth_header_name, auth_header_value_encrypted"
                " FROM product.tenant_custom_mcp_servers"
                " WHERE id = %s AND user_id = %s AND status != 'disabled'",
                (server_id, user["user_id"]),
            )
            row = cur.fetchone()
    except Exception as e:
        logger.error(f"tenant MCP server reverify lookup failed: {e}")
        raise HTTPException(status_code=503, detail="Tenant MCP server store unavailable")
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Server not found")
    agent_type, url, auth_header_name, encrypted = row
    _require_pro_or_above(user["user_id"])
    _require_cerveau_engine(user["user_id"], agent_type)

    auth_header_value = None
    if encrypted is not None:
        try:
            auth_header_value = mcp_server_encryption.decrypt_auth_header_value(bytes(encrypted))
        except Exception as e:
            logger.error(f"tenant MCP server auth header decrypt failed for {server_id}: {e}")

    return _verify_and_persist(server_id, user["user_id"], agent_type, url, auth_header_name, auth_header_value)


@router.delete("/{server_id}")
def disable_server(server_id: str, user: dict = Depends(get_current_user_payload)):
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE product.tenant_custom_mcp_servers
                SET status = 'disabled', disabled_at = now(), updated_at = now()
                WHERE id = %s AND user_id = %s AND status != 'disabled'
                RETURNING id
                """,
                (server_id, user["user_id"]),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            raise HTTPException(status_code=404, detail="Server not found or already disabled")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"tenant MCP server disable failed: {e}")
        raise HTTPException(status_code=503, detail="Tenant MCP server store unavailable")
    finally:
        conn.close()


# ============================================================================
# INTERNAL (Cerveau-facing)
# ============================================================================


@router.get("/internal/{user_id}/{agent_type}", dependencies=[Depends(require_internal_token)])
def internal_list_verified_servers(user_id: str, agent_type: str):
    _check_agent_type(agent_type)
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, url, transport, auth_header_name, auth_header_value_encrypted, risk_tier"
                " FROM product.tenant_custom_mcp_servers"
                " WHERE user_id = %s AND agent_type = %s AND status = 'verified'",
                (user_id, agent_type),
            )
            rows = cur.fetchall()
    except Exception as e:
        logger.error(f"internal tenant MCP server lookup failed for {user_id}/{agent_type}: {e}")
        raise HTTPException(status_code=503, detail="Tenant MCP server store unavailable")
    finally:
        conn.close()

    servers = []
    for name, url, transport, auth_header_name, encrypted, risk_tier in rows:
        auth_header_value = None
        if encrypted is not None:
            try:
                auth_header_value = mcp_server_encryption.decrypt_auth_header_value(bytes(encrypted))
            except Exception as e:
                logger.error(f"internal tenant MCP server decrypt failed for {user_id}/{agent_type}/{name}: {e}")
                continue  # a server whose auth header can't be decrypted must not be handed out
        servers.append(
            {
                "name": name,
                "url": url,
                "transport": transport,
                "auth_header_name": auth_header_name,
                "auth_header_value": auth_header_value,
                "risk_tier": risk_tier,
            }
        )
    return {"servers": servers}

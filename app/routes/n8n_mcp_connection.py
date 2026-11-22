"""
n8n MCP connection — one per user (docs/N8N-MCP-CONNECTION-PLAN-2026-11.md).

Lets a user connect their own n8n instance's built-in instance-level MCP
server (`<n8n>/mcp-server/http`) so their agents can find and run the
workflows they exposed there. It is a *user-level* connection, unlike
`tenant_mcp_servers` which is registered per (user_id, agent_type): the user
connects n8n once, then chooses which of their agents may use it.

Runtime stays unchanged. Cerveau already reads a tenant's custom MCP servers
from `GET /api/v1/tenant-mcp-servers/internal/{user_id}/{agent_type}`; that
endpoint calls `internal_servers_for_agent` below and appends this user's
n8n connection (as the synthetic server `aivory-n8n`) when the agent is
allowed to use it. Verification and the runtime transport therefore both go
through the existing SSRF-guarded paths (`guarded_fetch`, Cerveau's
`guarded_resolve`).

Access gate: usable credits, not plan tier (decision 2026-11-22). A paid
plan, a top-up balance or a superadmin all qualify; an account with nothing
to spend does not. Agent turns already spend credits at run time, so this
gate only decides who may *connect*.

Tool exposure is an allowlist, never a denylist. n8n's MCP server offers ~28
tools on 2.26 (credentials, data tables, publish/unpublish, archive, create
and update workflow, ...), and one bearer token unlocks all of them. Phase 1
exposes only five read/run tools; anything else — including tools a future
n8n version adds — stays off. The user may switch phase-1 tools off, never
on beyond that set.

  GET    /api/v1/n8n-mcp-connection             -> the connection (never the token)
  PUT    /api/v1/n8n-mcp-connection             -> connect / replace (url + token), verifies
  POST   /api/v1/n8n-mcp-connection/reverify    -> re-run verification with the stored token
  PUT    /api/v1/n8n-mcp-connection/token       -> replace the token only, verifies
  PUT    /api/v1/n8n-mcp-connection/tools       -> choose enabled tools (subset of phase 1)
  PUT    /api/v1/n8n-mcp-connection/agents      -> choose which agents may use it
  DELETE /api/v1/n8n-mcp-connection             -> disconnect
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.routes.agent_actions import get_current_user_payload
from app.routes.agent_profiles import AGENT_TYPES
from app.routes.tenant_mcp_servers import (
    _MAX_ERROR_LEN,
    _connect,
    _db_service,
    _require_cerveau_engine,
    _run_verification,
    _validate_https_url,
)
from app.services import credit_service, mcp_server_encryption
from app.services.guarded_fetch import GuardedFetchError
from app.services.telegram_service import is_superadmin, load_user_record

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/n8n-mcp-connection", tags=["n8n-mcp-connection"])

# Name the connection carries in the server list Cerveau reads. Hyphenated and
# `aivory-` prefixed like the other first-party connectors, so it cannot clash
# with a name a tenant picked for a custom server (the merge skips on a clash
# anyway).
N8N_SERVER_NAME = "aivory-n8n"

# n8n's instance-level MCP bearer token is an Authorization header.
_AUTH_HEADER_NAME = "Authorization"

# The only tools phase 1 exposes: find workflows, read one, run one, read the
# result. Names are n8n 2.26's. A tool outside this set is never enabled, by
# the user or by default — see `_effective_disabled_tools`.
PHASE1_TOOLS = (
    "search_workflows",
    "get_workflow_details",
    "execute_workflow",
    "get_execution",
    "search_executions",
)

# Running a workflow executes the user's own automation on their instruction,
# the same posture as the other custom servers ("the user's explicit
# instruction IS the approval", tenant_mcp_servers.py). A row can be flipped to
# 'irreversible' to park every call for approval if that is ever wanted.
_DEFAULT_RISK_TIER = "reversible"

# Verification calls the user's own URL from our backend. Even behind
# guarded_fetch that is an outbound request on demand, so cap how often one
# user can trigger it. Per process and best effort, like the auth-failure
# dedupe in tenant_mcp_servers.
_VERIFY_WINDOW_SECONDS = 600
_VERIFY_MAX_PER_WINDOW = 10
_verify_attempts: dict[str, list[float]] = {}
_verify_lock = threading.Lock()

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS product.user_n8n_mcp_connections (
    user_id                       TEXT PRIMARY KEY,
    url                           TEXT NOT NULL,
    auth_header_value_encrypted   BYTEA NOT NULL,
    status                        TEXT NOT NULL DEFAULT 'pending_verification',
    risk_tier                     TEXT NOT NULL DEFAULT 'reversible',
    last_verified_at              TIMESTAMPTZ,
    last_verify_error             TEXT,
    tool_count                    INTEGER,
    tools_json                    JSONB NOT NULL DEFAULT '[]',
    enabled_tools                 TEXT[] NOT NULL DEFAULT '{}',
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_n8n_mcp_connections_status_check
        CHECK (status IN ('pending_verification','verified','verification_failed')),
    CONSTRAINT user_n8n_mcp_connections_risk_tier_check
        CHECK (risk_tier IN ('safe','reversible','irreversible'))
);
CREATE TABLE IF NOT EXISTS product.user_n8n_mcp_connection_agents (
    user_id     TEXT NOT NULL,
    agent_type  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, agent_type),
    FOREIGN KEY (user_id) REFERENCES product.user_n8n_mcp_connections (user_id) ON DELETE CASCADE
);
"""

_schema_ready = False


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_SQL)
    conn.commit()
    _schema_ready = True


# ── Aivory's own n8n is not a user's n8n ────────────────────────────────────
#
# Aivory runs its own n8n (n8n.aivory.id). It is reachable from the internet, so
# guarded_fetch's private-address check does not keep a user from pointing this
# connection at it, and its owner-level tooling must not be reachable through a
# user's agent. The REST path already restricts it to superadmins
# (`canUseAivoryN8nInstance` in the dashboard); this is the same rule for MCP.
# Superadmins keep access so the instance can still be used for testing.
_AIVORY_DOMAINS = ("aivory.id", "aivory.uk")
_AIVORY_N8N_HOST_ENV_VARS = ("N8N_URL", "N8N_BASE_URL", "N8N_EDITOR_BASE_URL", "WEBHOOK_URL", "N8N_WEBHOOK_URL")


def _host_of(url: str) -> str:
    try:
        host = urlsplit(url.strip()).hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


def _is_aivory_n8n_host(url: str) -> bool:
    """True for any host under Aivory's own domains and for the hosts our
    deployment is configured with. Matches whole labels only, so
    `evil-aivory.id` and `n8n.aivory.id.evil.com` are not Aivory's."""
    host = _host_of(url)
    if not host:
        return False
    if any(host == d or host.endswith("." + d) for d in _AIVORY_DOMAINS):
        return True
    configured = {_host_of(os.getenv(name, "")) for name in _AIVORY_N8N_HOST_ENV_VARS}
    extra = {h.strip().lower().rstrip(".") for h in os.getenv("N8N_MCP_BLOCKED_HOSTS", "").split(",")}
    return host in (configured | extra) - {""}


def _reject_aivory_instance(url: str, user_id: str) -> None:
    if not _is_aivory_n8n_host(url):
        return
    record = load_user_record(_db_service, user_id) or {"user_id": user_id}
    if is_superadmin(record):
        return
    raise HTTPException(
        status_code=403,
        detail="This n8n instance belongs to Aivory and cannot be connected. Connect your own n8n instance.",
    )


# ── Gate and rate limit ─────────────────────────────────────────────────────


def _require_usable_credits(user_id: str) -> None:
    """Connect only with credits to spend (or as a superadmin, who is
    unlimited). The plan tier is deliberately not consulted: a one-time buyer
    or a top-up-only account has no live plan but can have credits, and a
    paid plan with an empty balance cannot run an agent anyway."""
    try:
        status = credit_service.get_status(user_id)
    except Exception as e:
        logger.error(f"n8n MCP connection credit check failed for {user_id}: {e}")
        raise HTTPException(status_code=503, detail="Credit service unavailable")
    if status.get("unlimited"):
        return
    if (status.get("balance") or 0) <= 0:
        raise HTTPException(
            status_code=402,
            detail="Connecting n8n needs credits. Top up or upgrade your plan to connect it.",
        )


def _check_verify_rate(user_id: str, now: Optional[float] = None) -> None:
    now = time.monotonic() if now is None else now
    with _verify_lock:
        recent = [t for t in _verify_attempts.get(user_id, []) if now - t < _VERIFY_WINDOW_SECONDS]
        if len(recent) >= _VERIFY_MAX_PER_WINDOW:
            _verify_attempts[user_id] = recent
            raise HTTPException(
                status_code=429,
                detail="Too many connection checks. Wait a few minutes and try again.",
            )
        recent.append(now)
        _verify_attempts[user_id] = recent


# ── Tool allowlist ──────────────────────────────────────────────────────────


def _default_enabled_tools(tool_names: list[str]) -> list[str]:
    """Phase-1 tools the server actually offers, in n8n's own order."""
    return [n for n in tool_names if n in PHASE1_TOOLS]


def _effective_enabled(enabled_tools: list[str]) -> list[str]:
    # Belt and braces: even if a row somehow holds a tool outside phase 1
    # (manual edit, a later version narrowing the set), it is never exposed.
    return [t for t in enabled_tools if t in PHASE1_TOOLS]


def _effective_disabled_tools(tools_json: list, enabled_tools: list[str]) -> list[str]:
    """What Cerveau gets as `disabled_tools`. Everything the server offers
    that is not explicitly enabled — an allowlist expressed as the denylist
    the runtime understands. A tool the server adds later is not in
    `tools_json` until the next verification, and then it is off by default."""
    enabled = set(_effective_enabled(enabled_tools))
    return [t["name"] for t in tools_json if isinstance(t, dict) and t.get("name") and t["name"] not in enabled]


# ── Models ──────────────────────────────────────────────────────────────────


def _normalize_token(raw: str) -> str:
    token = raw.strip()
    if token.lower() == "bearer":  # the scheme pasted with nothing after it
        return ""
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


class ConnectRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    token: str = Field(min_length=1, max_length=4000)

    @field_validator("token")
    @classmethod
    def _token(cls, v: str) -> str:
        token = _normalize_token(v)
        if not token or any(c.isspace() for c in token):
            raise ValueError("token must be a single access token")
        return token


class TokenRequest(BaseModel):
    token: str = Field(min_length=1, max_length=4000)

    @field_validator("token")
    @classmethod
    def _token(cls, v: str) -> str:
        token = _normalize_token(v)
        if not token or any(c.isspace() for c in token):
            raise ValueError("token must be a single access token")
        return token


class ToolsRequest(BaseModel):
    enabled_tools: list[str] = Field(max_length=len(PHASE1_TOOLS))

    @field_validator("enabled_tools")
    @classmethod
    def _subset(cls, v: list[str]) -> list[str]:
        unknown = [t for t in v if t not in PHASE1_TOOLS]
        if unknown:
            raise ValueError(f"tools not available yet: {', '.join(sorted(set(unknown)))}")
        return list(dict.fromkeys(v))


class AgentsRequest(BaseModel):
    agent_types: list[str] = Field(max_length=50)

    @field_validator("agent_types")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        unknown = [a for a in v if a not in AGENT_TYPES]
        if unknown:
            raise ValueError(f"unknown agent type: {', '.join(sorted(set(unknown)))}")
        return list(dict.fromkeys(v))


# ── Persistence helpers ─────────────────────────────────────────────────────

_COLUMNS = (
    "url, status, risk_tier, last_verified_at, last_verify_error, tool_count, tools_json,"
    " enabled_tools, created_at, updated_at"
)


def _row_to_public(row, agent_types: list[str]) -> dict:
    (
        url,
        status,
        risk_tier,
        last_verified_at,
        last_verify_error,
        tool_count,
        tools_json,
        enabled_tools,
        created_at,
        updated_at,
    ) = row
    enabled = _effective_enabled(list(enabled_tools or []))
    tools = []
    for t in tools_json or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        tools.append(
            {
                "name": t["name"],
                "description": t.get("description", ""),
                # Whether the user can ever turn it on in this version, and
                # whether it is on now.
                "available": t["name"] in PHASE1_TOOLS,
                "enabled": t["name"] in enabled,
            }
        )
    return {
        "url": url,
        "status": status,
        "risk_tier": risk_tier,
        "last_verified_at": last_verified_at.isoformat() if last_verified_at else None,
        "last_verify_error": last_verify_error,
        "tool_count": tool_count,
        "tools": tools,
        "enabled_tools": enabled,
        "agent_types": agent_types,
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


def _load(cur, user_id: str):
    cur.execute(f"SELECT {_COLUMNS} FROM product.user_n8n_mcp_connections WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    if row is None:
        return None
    cur.execute(
        "SELECT agent_type FROM product.user_n8n_mcp_connection_agents WHERE user_id = %s ORDER BY agent_type",
        (user_id,),
    )
    return _row_to_public(row, [r[0] for r in cur.fetchall()])


def _verify(url: str, token: str) -> tuple[str, Optional[str], list]:
    """(status, error, tools). Never raises: a failed handshake is a state the
    caller persists and reports, not a crash."""
    try:
        result = _run_verification(url, _AUTH_HEADER_NAME, f"Bearer {token}")
        return "verified", None, result["tools"]
    except GuardedFetchError as e:
        return "verification_failed", str(e)[:_MAX_ERROR_LEN], []
    except Exception as e:
        logger.error(f"n8n MCP verification crashed: {e}")
        return "verification_failed", "verification failed unexpectedly", []


def _persist_verification(
    user_id: str,
    status: str,
    error: Optional[str],
    tools: list,
    *,
    url: Optional[str] = None,
    encrypted_token: Optional[bytes] = None,
) -> dict:
    """Write the outcome of a verification. With `url` and `encrypted_token`
    it is an upsert (connect / replace); without, it updates the existing row
    (reverify / new token)."""
    verified = status == "verified"
    names = [t["name"] for t in tools]
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            if url is not None and encrypted_token is not None:
                # A fresh connect starts from the safe default. Re-connecting
                # over an existing row keeps the user's earlier tool choices
                # that are still valid, and the agent grants.
                cur.execute(
                    """
                    INSERT INTO product.user_n8n_mcp_connections
                        (user_id, url, auth_header_value_encrypted, status, risk_tier,
                         last_verified_at, last_verify_error, tool_count, tools_json, enabled_tools)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET
                        url = EXCLUDED.url,
                        auth_header_value_encrypted = EXCLUDED.auth_header_value_encrypted,
                        status = EXCLUDED.status,
                        last_verified_at = EXCLUDED.last_verified_at,
                        last_verify_error = EXCLUDED.last_verify_error,
                        tool_count = EXCLUDED.tool_count,
                        tools_json = EXCLUDED.tools_json,
                        updated_at = now()
                    """,
                    (
                        user_id,
                        url,
                        encrypted_token,
                        status,
                        _DEFAULT_RISK_TIER,
                        datetime.now(timezone.utc) if verified else None,
                        error,
                        len(tools) if verified else None,
                        json.dumps(tools if verified else []),
                        _default_enabled_tools(names) if verified else [],
                    ),
                )
            else:
                cur.execute(
                    """
                    UPDATE product.user_n8n_mcp_connections
                    SET status = %s, last_verified_at = %s, last_verify_error = %s,
                        tool_count = %s, tools_json = %s, updated_at = now()
                    WHERE user_id = %s
                    """,
                    (
                        status,
                        datetime.now(timezone.utc) if verified else None,
                        error,
                        len(tools) if verified else None,
                        json.dumps(tools if verified else []),
                        user_id,
                    ),
                )
            if verified:
                # First successful verification (or one after a failure that
                # left the list empty): fill in the default allowlist. Later
                # verifications keep the user's choices.
                cur.execute(
                    """
                    UPDATE product.user_n8n_mcp_connections
                    SET enabled_tools = %s
                    WHERE user_id = %s AND cardinality(enabled_tools) = 0
                    """,
                    (_default_enabled_tools(names), user_id),
                )
            public = _load(cur, user_id)
        conn.commit()
    except Exception as e:
        logger.error(f"n8n MCP connection persist failed for {user_id}: {e}")
        raise HTTPException(status_code=503, detail="n8n connection store unavailable")
    finally:
        conn.close()
    return public


def _fail_or_return(public: dict, status: str, error: Optional[str]) -> dict:
    if status == "verified":
        return public
    raise HTTPException(status_code=422, detail={"error": "verification_failed", "reason": error, "connection": public})


def _load_secret(user_id: str) -> tuple[str, str]:
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT url, auth_header_value_encrypted FROM product.user_n8n_mcp_connections WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
    except Exception as e:
        logger.error(f"n8n MCP connection lookup failed for {user_id}: {e}")
        raise HTTPException(status_code=503, detail="n8n connection store unavailable")
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="No n8n connection")
    url, encrypted = row
    try:
        header_value = mcp_server_encryption.decrypt_auth_header_value(bytes(encrypted))
    except Exception as e:
        logger.error(f"n8n MCP connection decrypt failed for {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Stored token could not be read; reconnect n8n")
    # Stored as the full header value ("Bearer <token>"); callers want the token.
    return url, _normalize_token(header_value)


# ── Routes ──────────────────────────────────────────────────────────────────


@router.get("")
def get_connection(user: dict = Depends(get_current_user_payload)):
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            public = _load(cur, user["user_id"])
        conn.commit()
    except Exception as e:
        logger.error(f"n8n MCP connection read failed for {user['user_id']}: {e}")
        raise HTTPException(status_code=503, detail="n8n connection store unavailable")
    finally:
        conn.close()
    return {"connection": public, "phase1_tools": list(PHASE1_TOOLS)}


@router.put("")
def connect(body: ConnectRequest, user: dict = Depends(get_current_user_payload)):
    user_id = user["user_id"]
    _validate_https_url(body.url)
    _reject_aivory_instance(body.url, user_id)
    _require_usable_credits(user_id)
    _check_verify_rate(user_id)
    status, error, tools = _verify(body.url, body.token)
    encrypted = mcp_server_encryption.encrypt_auth_header_value(f"Bearer {body.token}")
    public = _persist_verification(user_id, status, error, tools, url=body.url, encrypted_token=encrypted)
    return _fail_or_return(public, status, error)


@router.post("/reverify")
def reverify(user: dict = Depends(get_current_user_payload)):
    user_id = user["user_id"]
    _require_usable_credits(user_id)
    url, token = _load_secret(user_id)
    _reject_aivory_instance(url, user_id)
    _check_verify_rate(user_id)
    status, error, tools = _verify(url, token)
    public = _persist_verification(user_id, status, error, tools)
    return _fail_or_return(public, status, error)


@router.put("/token")
def replace_token(body: TokenRequest, user: dict = Depends(get_current_user_payload)):
    user_id = user["user_id"]
    _require_usable_credits(user_id)
    url, _old = _load_secret(user_id)
    _reject_aivory_instance(url, user_id)
    _check_verify_rate(user_id)
    status, error, tools = _verify(url, body.token)
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE product.user_n8n_mcp_connections SET auth_header_value_encrypted = %s, updated_at = now()"
                " WHERE user_id = %s",
                (mcp_server_encryption.encrypt_auth_header_value(f"Bearer {body.token}"), user_id),
            )
        conn.commit()
    except Exception as e:
        logger.error(f"n8n MCP token update failed for {user_id}: {e}")
        raise HTTPException(status_code=503, detail="n8n connection store unavailable")
    finally:
        conn.close()
    public = _persist_verification(user_id, status, error, tools)
    return _fail_or_return(public, status, error)


@router.put("/tools")
def set_tools(body: ToolsRequest, user: dict = Depends(get_current_user_payload)):
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE product.user_n8n_mcp_connections SET enabled_tools = %s, updated_at = now()"
                " WHERE user_id = %s RETURNING user_id",
                (body.enabled_tools, user["user_id"]),
            )
            if cur.fetchone() is None:
                conn.rollback()
                raise HTTPException(status_code=404, detail="No n8n connection")
            public = _load(cur, user["user_id"])
        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"n8n MCP tools update failed for {user['user_id']}: {e}")
        raise HTTPException(status_code=503, detail="n8n connection store unavailable")
    finally:
        conn.close()
    return public


@router.put("/agents")
def set_agents(body: AgentsRequest, user: dict = Depends(get_current_user_payload)):
    user_id = user["user_id"]
    # Same requirement as any custom MCP server: the agent must run on
    # Cerveau, otherwise the tools would never reach it.
    for agent_type in body.agent_types:
        _require_cerveau_engine(user_id, agent_type)
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM product.user_n8n_mcp_connections WHERE user_id = %s", (user_id,))
            if cur.fetchone() is None:
                conn.rollback()
                raise HTTPException(status_code=404, detail="No n8n connection")
            cur.execute("DELETE FROM product.user_n8n_mcp_connection_agents WHERE user_id = %s", (user_id,))
            for agent_type in body.agent_types:
                cur.execute(
                    "INSERT INTO product.user_n8n_mcp_connection_agents (user_id, agent_type) VALUES (%s, %s)",
                    (user_id, agent_type),
                )
            public = _load(cur, user_id)
        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"n8n MCP agents update failed for {user_id}: {e}")
        raise HTTPException(status_code=503, detail="n8n connection store unavailable")
    finally:
        conn.close()
    return public


@router.delete("", status_code=204)
def disconnect(user: dict = Depends(get_current_user_payload)):
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM product.user_n8n_mcp_connections WHERE user_id = %s", (user["user_id"],))
        conn.commit()
    except Exception as e:
        logger.error(f"n8n MCP disconnect failed for {user['user_id']}: {e}")
        raise HTTPException(status_code=503, detail="n8n connection store unavailable")
    finally:
        conn.close()
    return None


# ── Hooks used by tenant_mcp_servers' internal (Cerveau-facing) routes ──────


def internal_servers_for_agent(user_id: str, agent_type: str) -> tuple[list[dict], list[dict]]:
    """(servers, unavailable) to append to the tenant's custom-server list for
    this agent. Empty when the user has no n8n connection or has not granted it
    to this agent. Never raises: the n8n connection is an addition, and a
    problem here must not take down the agent's other servers."""
    try:
        conn = _connect()
    except Exception as e:
        logger.error(f"n8n MCP internal lookup could not connect: {e}")
        return [], []
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.url, c.auth_header_value_encrypted, c.status, c.risk_tier, c.tools_json,
                       c.enabled_tools, c.last_verify_error
                FROM product.user_n8n_mcp_connections c
                JOIN product.user_n8n_mcp_connection_agents a ON a.user_id = c.user_id
                WHERE c.user_id = %s AND a.agent_type = %s
                """,
                (user_id, agent_type),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception as e:
        logger.error(f"n8n MCP internal lookup failed for {user_id}/{agent_type}: {e}")
        return [], []
    finally:
        conn.close()
    if row is None:
        return [], []
    url, encrypted, status, risk_tier, tools_json, enabled_tools, last_error = row
    if status != "verified":
        # Same shape as a failed custom server: the agent is told it is down.
        code = "auth_rejected" if last_error and ("HTTP 401" in last_error or "HTTP 403" in last_error) else "verification_failed"
        return [], [{"name": N8N_SERVER_NAME, "reason": code}]
    try:
        auth_value = mcp_server_encryption.decrypt_auth_header_value(bytes(encrypted))
    except Exception as e:
        logger.error(f"n8n MCP internal decrypt failed for {user_id}: {e}")
        return [], []  # a connection whose token cannot be decrypted is not handed out
    return (
        [
            {
                "name": N8N_SERVER_NAME,
                "url": url,
                "transport": "streamable-http",
                "auth_header_name": _AUTH_HEADER_NAME,
                "auth_header_value": auth_value,
                "risk_tier": risk_tier,
                "disabled_tools": _effective_disabled_tools(tools_json or [], list(enabled_tools or [])),
            }
        ],
        [],
    )


def mark_auth_rejected(user_id: str, status: int) -> bool:
    """Cerveau got 401/403 from the user's n8n: flip the connection to
    `verification_failed` so the dashboard stops showing it as healthy. Returns
    whether a row changed. A connection verified in the last few minutes is left
    alone (Cerveau caches the server list, so it can still present an old token
    once right after the user replaced it)."""
    reason = (
        f"Aivory's agent was refused with HTTP {status}: the n8n access token has expired or been "
        "revoked. Generate a new token in n8n and replace it here."
    )
    try:
        conn = _connect()
    except Exception as e:
        logger.error(f"n8n MCP auth-failure report could not connect: {e}")
        return False
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE product.user_n8n_mcp_connections
                SET status = 'verification_failed', last_verify_error = %s, updated_at = now()
                WHERE user_id = %s AND status = 'verified'
                  AND (last_verified_at IS NULL OR last_verified_at < now() - interval '6 minutes')
                RETURNING user_id
                """,
                (reason, user_id),
            )
            flipped = cur.fetchone() is not None
        conn.commit()
        return flipped
    except Exception as e:
        logger.error(f"n8n MCP auth-failure report failed for {user_id}: {e}")
        return False
    finally:
        conn.close()

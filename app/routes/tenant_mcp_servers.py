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
    PATCH  /api/v1/tenant-mcp-servers/{id}/tools     -> set per-tool disabled_tools (§B8)
    DELETE /api/v1/tenant-mcp-servers/{id}        -> disable

Internal (Cerveau-facing, X-Internal-Token):
    GET /api/v1/tenant-mcp-servers/internal/{user_id}/{agent_type}
        -> decrypted, status='verified' rows only

Registration quota is per (user_id, agent_type) and scales with the plan:
Operational 1, Business 3, Enterprise 10 (`_MAX_SERVERS_BY_TIER`). A
superadmin resolves to Enterprise, matching auth_service._compute_tier.

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
import secrets
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from app.database.db_service import DatabaseService
from app.routes.agent_actions import get_current_user_payload, require_internal_token
from app.routes.agent_profiles import AGENT_TYPES, load_profile_internal
from app.routes.agent_roster import AGENT_ROSTER
from app.services import mcp_server_encryption, tiers
from app.services.guarded_fetch import GuardedFetchError, guarded_fetch
from app.services.telegram_service import is_superadmin, load_user_record

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tenant-mcp-servers", tags=["tenant-mcp-servers"])

# Module-level, matches app/routes/telegram.py / agent_api_keys.py's own convention.
_db_service = DatabaseService()

# Tier ladder follows the 2026 pricing rebrand (Operational / Business /
# Enterprise). The ladder and the legacy-name aliases now live in
# app/services/tiers.py rather than being re-declared per route.
#
# `operational` is the first paid rung. An account with no live plan resolves
# to `tiers.FREE_TIER`, which ranks below it, so this gate now rejects
# non-paying and lapsed callers. It previously admitted everyone: the old
# ladder gave unknown tiers rung 0, the same rung as `operational`, and
# load_user_record resolved a missing or lapsed entitlement to the base PAID
# tier.
_MIN_TIER = "operational"

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")
_MAX_ERROR_LEN = 500

# Per-agent registration quota, by plan. The original v1 cap was a flat 1 for
# every caller, which made the cheapest paid rung and Enterprise identical on
# the one axis this feature actually scales along — and left a superadmin
# unable to register a second server at all. The schema always supported more
# than one row per (user_id, agent_type); only route logic capped it.
#
# `enterprise` is deliberately bounded rather than unlimited: every tool on a
# tenant-supplied server is a black box Aivory never reviewed (§B5), so the
# blast radius of one account stays finite.
_MAX_SERVERS_BY_TIER = {
    "operational": 1,
    "business": 3,
    "enterprise": 10,
}

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
    -- Approval-gate removal (2026-09-17, owner decision): the user's
    -- explicit instruction IS the approval (draft preview + in-conversation
    -- confirm, no second ask from the gate). New servers default to 'safe'
    -- (never park); a server can still be flipped back to 'irreversible'
    -- per-row if gating is ever wanted again for that system.
    risk_tier                     TEXT NOT NULL DEFAULT 'safe',
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
"""

# Per-tool allow/deny (§B8): added after the table above already shipped, so
# these ride in as an idempotent ALTER rather than the CREATE TABLE body —
# `tools_json` is the last-verified {name, description} list (the dashboard
# needs it to render checkboxes without re-verifying on every page load);
# `disabled_tools` is the tenant's own denylist against that list, applied
# by Cerveau's `TenantCustomMcpServer::disabled_tools` at MCP-connect time
# (never by name lookup at call time — a disabled tool is never advertised
# to the model in the first place, same posture as `disabled_toolkits`).
_MIGRATE_SQL = """
ALTER TABLE product.tenant_custom_mcp_servers
    ADD COLUMN IF NOT EXISTS tools_json JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS disabled_tools TEXT[] NOT NULL DEFAULT '{}';
"""

# Approval-gate removal (2026-09-17, owner decision): previously 'irreversible'
# by default, so every tenant-supplied tool parked as Pending even after an
# explicit user instruction. New default is 'safe' (never park) for generic
# custom servers; a server can still be flipped back to 'irreversible'
# per-row if gating is ever wanted again for that system.
# P0 Odoo exception (2026-09-21): rows named 'odoo' (shared Od-MCP) stay
# 'irreversible' -- ERP writes must park for approval (ADR-006 B5, ADR-012
# non-goals). The boot migration below exempts them so a restart can never
# silently ungate Odoo writes.
_RISK_TIER_MIGRATE_SQL = """
ALTER TABLE product.tenant_custom_mcp_servers
    ALTER COLUMN risk_tier SET DEFAULT 'safe';
UPDATE product.tenant_custom_mcp_servers
    SET risk_tier = 'safe', updated_at = now()
    WHERE status != 'disabled' AND risk_tier = 'irreversible' AND name != 'odoo';
"""

# The original index (still created by some already-deployed instances of
# _SCHEMA_SQL, before this comment existed) was a plain unique index on
# (user_id, agent_type, name) with no WHERE clause. Combined with `disable`
# being a soft-delete (status='disabled', row kept for audit), that made a
# disabled server's name permanently unusable: re-registering the same name
# — the only name some cards let you pick, e.g. the fixed "aivory-mail" card
# — always hit the same unique-violation the first registration did, no
# matter how long ago it was disabled. Drop-then-recreate as a partial index
# so only non-disabled rows compete for a name; safe to run on every boot.
_INDEX_MIGRATE_SQL = """
DROP INDEX IF EXISTS product.tenant_custom_mcp_servers_user_agent_name_idx;
CREATE UNIQUE INDEX IF NOT EXISTS tenant_custom_mcp_servers_user_agent_name_idx
    ON product.tenant_custom_mcp_servers (user_id, agent_type, name)
    WHERE status != 'disabled';
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
        cur.execute(_MIGRATE_SQL)
        cur.execute(_RISK_TIER_MIGRATE_SQL)
        cur.execute(_INDEX_MIGRATE_SQL)
    conn.commit()
    _schema_ready = True


def _check_agent_type(agent_type: str) -> None:
    if agent_type not in AGENT_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown agent type '{agent_type}'")


def _require_paid_tier(user_id: str) -> str:
    # The JWT payload never carries `tier` (create_access_token only bakes in
    # user_id/email/account_type) — every real tier check must re-load the
    # current record from Postgres, same pattern telegram.py's agent_chat
    # already uses. Checking a claim off the raw JWT here would 403 every
    # real non-superadmin caller regardless of their actual plan.
    record = load_user_record(_db_service, user_id) or {"user_id": user_id}
    if is_superadmin(record):
        # A superadmin holds the Enterprise feature set everywhere else
        # (auth_service._compute_tier returns tier="enterprise" for one), so
        # it resolves to Enterprise here too rather than to a bare pass.
        return "enterprise"
    tier = tiers.account_tier(record.get("tier"))
    if not tiers.meets(tier, _MIN_TIER):
        raise HTTPException(
            status_code=403,
            detail="Custom MCP servers are available on paid plans (Operational, Business, or Enterprise). Upgrade to register one.",
        )
    return tier


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


def _mcp_jsonrpc_call(url: str, method: str, params: dict, headers: dict, request_id: int) -> tuple[dict, Optional[str]]:
    """Returns (result, session_id) — session_id is the server's
    Mcp-Session-Id response header, if it sent one. The streamable-http
    transport is stateful: a server that issues a session id on `initialize`
    (the MCP spec's expected behavior, and what real servers like
    erpipe-org/mcp-odoo do) will 400 "Missing session ID" on every later
    call in the same handshake unless that header is echoed back — this
    verification handshake didn't do that until 2026-08-26, so it could
    never actually pass against a real session-issuing MCP server."""
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
    session_id = next((v for k, v in resp.headers.items() if k.lower() == "mcp-session-id"), None)
    return parsed.get("result") or {}, session_id


_ODOO_KEY_REJECTED_REASON = (
    "Odoo rejected the API key: it has expired or been revoked. In Odoo, open "
    "Preferences > Account Security > New API Key, choose a longer duration, "
    "then reconnect Odoo here."
)


def _is_shared_odoo_url(url: str) -> bool:
    return url.startswith(f"{_SHARED_ODOO_MCP_BASE_URL}/mcp")


def _probe_shared_odoo(url: str, headers: dict) -> None:
    """One real read against the tenant's Odoo through Od-MCP.

    initialize + tools/list never touch Odoo (Od-MCP answers them itself),
    so a wrong, expired or revoked API key used to verify fine and then
    401 on every agent call. Odoo 19 API keys default to short durations,
    so this is the common failure, not a corner case (seen 2026-09-25: a
    1-day key expired overnight while the card still said verified)."""
    try:
        result, _ = _mcp_jsonrpc_call(
            url,
            "tools/call",
            {"name": "odoo_check_access", "arguments": {"model": "res.partner", "operation": "read"}},
            headers,
            request_id=3,
        )
    except GuardedFetchError as e:
        msg = str(e)
        lowered = msg.lower()
        if "401" in msg or "apikey" in lowered or "api key" in lowered or "access denied" in lowered:
            raise GuardedFetchError(_ODOO_KEY_REJECTED_REASON)
        raise GuardedFetchError(f"Odoo did not answer a test read: {msg[:300]}")
    if isinstance(result, dict) and result.get("isError"):
        raise GuardedFetchError("Odoo did not answer a test read through the connector.")


def _run_verification(url: str, auth_header_name: Optional[str], auth_header_value: Optional[str]) -> dict:
    """Real MCP initialize + tools/list handshake through the guarded
    fetcher. Returns {tools: [{name, description}]} on success. Raises
    GuardedFetchError (safe-to-display reason) on any failure — SSRF
    rejection, network failure, non-2xx, or malformed/error JSON-RPC.
    For Aivory's shared Od-MCP it also proves the Odoo credentials with one
    read (`_probe_shared_odoo`)."""
    headers = {}
    if auth_header_name and auth_header_value:
        headers[auth_header_name] = auth_header_value

    session_id = _run_verification_init(url, headers)
    if session_id:
        headers = {**headers, "Mcp-Session-Id": session_id}
    result, _ = _mcp_jsonrpc_call(url, "tools/list", {}, headers, request_id=2)
    raw_tools = result.get("tools")
    if not isinstance(raw_tools, list):
        raise GuardedFetchError("server did not return a tools list")
    if _is_shared_odoo_url(url):
        _probe_shared_odoo(url, headers)

    tools = []
    for t in raw_tools:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        tools.append({"name": str(t["name"])[:200], "description": str(t.get("description") or "")[:500]})
    return {"tools": tools}


def _run_verification_init(url: str, headers: dict) -> Optional[str]:
    _, session_id = _mcp_jsonrpc_call(
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
    return session_id


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

    @model_validator(mode="after")
    def _normalize_auth_header_value(self) -> "RegisterServerRequest":
        # Authorization is a "<scheme> <credential>" pair (RFC 7235), and
        # every setup guide we show (Aivory Mail's own settings page included)
        # tells the operator to enter "Bearer <token>" as the value — but a
        # bare token is the far easier thing to accidentally paste, and the
        # dashboard sends whatever lands in this field completely verbatim.
        # A token-shaped value with no space is *never* a valid Authorization
        # value on its own, so silently 401ing every such registration (a
        # perfectly valid token, rejected only because "Bearer " is missing)
        # is a paste-error tax with no compensating safety benefit.
        if (
            self.auth_header_value
            and self.auth_header_name
            and self.auth_header_name.strip().lower() == "authorization"
            and " " not in self.auth_header_value.strip()
        ):
            self.auth_header_value = f"Bearer {self.auth_header_value.strip()}"
        return self


_LIST_COLUMNS = (
    "id, agent_type, name, url, transport, auth_header_name, status,"
    " last_verified_at, last_verify_error, tool_count, created_at, tools_json, disabled_tools"
)


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
        tools_json,
        disabled_tools,
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
        # `tools_json` defaults to '[]' at the column level, but a row
        # written before this migration (or mid-verification) may still
        # hand back a bare `None` from some driver/version combos.
        "tools": tools_json if tools_json is not None else [],
        "disabled_tools": list(disabled_tools) if disabled_tools else [],
    }


@router.post("", status_code=201)
def register_server(body: RegisterServerRequest, user: dict = Depends(get_current_user_payload)):
    _check_agent_type(body.agent_type)
    tier = _require_paid_tier(user["user_id"])
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
            # .get, not [] — _require_paid_tier can only return a canonical
            # tier today, but a KeyError here would 500 a registration.
            quota = _MAX_SERVERS_BY_TIER.get(tier, 1)
            if active_count >= quota:
                plural = "server" if quota == 1 else "servers"
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Your {tiers.display_name(tier)} plan allows {quota} custom MCP {plural} "
                        f"per agent. Remove one before registering another."
                    ),
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
            # `disabled_tools` is intentionally left untouched by a
            # reverify — a name that no longer appears in the refreshed
            # tools_json is simply inert (nothing to match at connect
            # time), and a tool the tenant already turned off stays off if
            # the upstream server brings it back under the same name.
            cur.execute(
                f"""
                UPDATE product.tenant_custom_mcp_servers
                SET status = %s, last_verified_at = %s, last_verify_error = %s,
                    tool_count = %s, tools_json = %s, updated_at = now()
                WHERE id = %s
                RETURNING {_LIST_COLUMNS}
                """,
                (
                    status,
                    datetime.now(timezone.utc) if status == "verified" else None,
                    error,
                    len(tools) if status == "verified" else None,
                    json.dumps(tools if status == "verified" else []),
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
        return payload
    raise HTTPException(status_code=422, detail={"error": "verification_failed", "reason": error, "server": payload})


# ── Odoo self-serve connect (Aivory-hub88/Od-MCP shared server) ────────────
#
# Every other custom-MCP registration above asks the tenant to run their own
# server and paste its URL (docs/ODOO-MCP-SETUP-GUIDE.md's BYO model, still
# the only path for a truly custom system). Odoo is different now: Aivory
# runs its own multi-tenant Od-MCP server (odoo-mcp.aivory.uk) that can add
# a tenant's Odoo instance at runtime with no server restart and no effect
# on any other tenant's live session (see Od-MCP's `OdooPool.add_instance`
# and its `/admin/instances` route). So the tenant only ever gives Aivory
# their own Odoo URL + API key -- no Docker, no reverse proxy, no MCP URL.
#
# Isolation is enforced by Od-MCP itself: each tenant here gets a freshly
# generated bearer token bound ONLY to their own instance
# (`resolve_tenant` in Od-MCP's tenant.rs), carried as a `?token=` query
# param on the registered URL rather than an Authorization header, so it
# never collides with any other auth layer in front of that shared server.

# Overridable for staging. Cerveau's read-tool carve-out matches this exact
# host (odoo-mcp.aivory.uk), so a different host keeps every Odoo tool gated
# until Cerveau learns it too — it fails closed, never open.
_SHARED_ODOO_MCP_BASE_URL = os.getenv("OD_MCP_BASE_URL", "https://odoo-mcp.aivory.uk").rstrip("/")
_OD_MCP_ADMIN_TIMEOUT = 15


class ConnectOdooRequest(BaseModel):
    agent_type: str
    odoo_url: str = Field(min_length=1, max_length=500)
    # Required, not auto-detected: Odoo has no reliable unauthenticated way
    # to tell us the database name for a multi-DB instance, and guessing
    # wrong silently connects the agent to the wrong company's data. The
    # dashboard's own copy explains where to find it (Settings → General
    # Settings → Database Name, or the Odoo.sh/OEC.sh dashboard).
    odoo_db: str = Field(min_length=1, max_length=200)
    api_key: str = Field(min_length=1, max_length=2000)
    # Optional but required for PDF reports on Odoo 19+: /report/pdf is an
    # auth='user' controller, so a bearer API key alone is rejected there
    # (401 / HTML login page, not a PDF). Od-MCP falls back to cookie-session
    # auth via /web/session/authenticate using (db, login, password=api_key),
    # which needs the Odoo login (email) that owns the API key. JSON-2 CRUD
    # keeps working without it; only odoo_generate_report needs it.
    odoo_username: Optional[str] = Field(default=None, max_length=320)

    @field_validator("odoo_url")
    @classmethod
    def _validate_odoo_url(cls, v: str) -> str:
        # P0 hardening: https-only. An Odoo API key over plain http is a
        # bearer credential on the wire; custom MCP URLs already enforce
        # this in _validate_https_url. Odoo 19 serves TLS by default
        # (odoo.sh, self-hosted behind Traefik/Caddy) so this is not a
        # real deployment blocker.
        parts = urlsplit(v.strip())
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("odoo_url must be a valid https:// URL, e.g. https://yourcompany.odoo.com")
        return v.strip().rstrip("/")


def _od_mcp_admin_token() -> str:
    token = os.getenv("OD_MCP_ADMIN_TOKEN")
    if not token:
        raise HTTPException(status_code=503, detail="Odoo connect is not configured on this deployment.")
    return token


def _od_mcp_revoke_instance(instance_name: str) -> None:
    """Best-effort removal of one tenant instance from the shared Od-MCP
    server. Fail-open by design: revoke must never block a dashboard
    disable/connect retry (network blip, already-deleted instance, admin
    token rotation). The backend row is the source of truth for what Cerveau
    is offered; this just avoids orphaned live tokens on the shared server."""
    try:
        admin_token = os.getenv("OD_MCP_ADMIN_TOKEN")
        if not admin_token:
            return
        requests.delete(
            f"{_SHARED_ODOO_MCP_BASE_URL}/admin/instances/{instance_name}",
            headers={"X-Admin-Token": admin_token},
            timeout=_OD_MCP_ADMIN_TIMEOUT,
        )
    except Exception as e:
        logger.warning(f"Od-MCP revoke best-effort failed for {instance_name}: {e}")


def _roster_label(agent_type: str) -> Optional[str]:
    """ "Lex - Sales and Lead Agent" for a roster agent_type, else None."""
    for entry in AGENT_ROSTER:
        if entry["agent_type"] == agent_type:
            return f'{entry["name"]} - {entry["title"]}'
    return None


@router.post("/odoo/connect", status_code=201)
def connect_odoo(body: ConnectOdooRequest, user: dict = Depends(get_current_user_payload)):
    """Register the caller's own Odoo (URL + API key + optional login email)
    against Aivory's shared Od-MCP server, then run it through the exact same
    initialize + tools/list verification every other custom MCP server
    gets (`_verify_and_persist`) -- so a bad api_key or db name surfaces
    as a normal 'verification failed' reason, not a bespoke error shape,
    and Od-MCP's real client code (not a re-implementation here) is what
    actually proves the credentials work.

    `odoo_username` (login email) is optional for CRUD but required for
    `odoo_generate_report` on Odoo 19+: /report/pdf is auth='user', bearer
    alone is rejected, so Od-MCP falls back to session auth with
    (db, login, password=api_key). Rows named 'odoo' stay 'irreversible'
    so every write parks as a Cerveau F-1 pending approval (console +
    Telegram), never auto-executes."""
    _check_agent_type(body.agent_type)
    tier = _require_paid_tier(user["user_id"])
    _require_cerveau_engine(user["user_id"], body.agent_type)
    admin_token = _od_mcp_admin_token()

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
            quota = _MAX_SERVERS_BY_TIER.get(tier, 1)
            if active_count >= quota:
                plural = "server" if quota == 1 else "servers"
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Your {tiers.display_name(tier)} plan allows {quota} custom MCP {plural} "
                        f"per agent. Remove one before connecting another."
                    ),
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"odoo connect quota check failed for {user['user_id']}: {e}")
        raise HTTPException(status_code=503, detail="Tenant MCP server store unavailable")
    finally:
        conn.close()

    # Instance name must be unique per (tenant, agent_type) and satisfy
    # Od-MCP's own `valid_instance_name` (alnum/-/_ only) -- user_id is
    # already that shape (`user_<hex>`), agent_type is a fixed enum.
    instance_name = f"t_{user['user_id']}_{body.agent_type}"[:64]
    tenant_token = secrets.token_hex(24)

    # P0: best-effort cleanup of a stale instance from a previous failed
    # connect (same deterministic name) so a retry does not 409 against its
    # own orphan. Fail-open; the POST below is authoritative.
    _od_mcp_revoke_instance(instance_name)

    admin_payload: dict = {
        "name": instance_name,
        "url": body.odoo_url,
        "db": body.odoo_db,
        "api_key": body.api_key,
        "mcpToken": tenant_token,
    }
    # Forward the login email when the tenant gave it so Od-MCP's report-PDF
    # session fallback (/web/session/authenticate with password=api_key) can
    # run. Omit when absent — JSON-2 CRUD does not need it.
    if body.odoo_username and body.odoo_username.strip():
        admin_payload["username"] = body.odoo_username.strip()
    # Od-MCP signs its Odoo chatter notes with this ("Updated by Lex - Sales
    # and Lead Agent: ..."). Sent from the canonical roster so Od-MCP keeps no
    # copy of agent names that could go stale on a rename.
    agent_label = _roster_label(body.agent_type)
    if agent_label:
        admin_payload["agentLabel"] = agent_label

    try:
        admin_resp = requests.post(
            f"{_SHARED_ODOO_MCP_BASE_URL}/admin/instances",
            json=admin_payload,
            headers={"X-Admin-Token": admin_token},
            timeout=_OD_MCP_ADMIN_TIMEOUT,
        )
    except requests.RequestException as e:
        logger.error(f"Od-MCP admin call failed for {user['user_id']}/{body.agent_type}: {e}")
        raise HTTPException(status_code=502, detail="Could not reach Aivory's Odoo connector service. Please try again.")

    if admin_resp.status_code >= 400:
        logger.error(f"Od-MCP admin rejected instance add for {user['user_id']}/{body.agent_type}: {admin_resp.status_code} {admin_resp.text[:200]}")
        raise HTTPException(status_code=502, detail="Could not register your Odoo instance. Please try again.")

    mcp_url = f"{_SHARED_ODOO_MCP_BASE_URL}/mcp?token={tenant_token}"

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO product.tenant_custom_mcp_servers
                    (user_id, agent_type, name, url, transport, auth_header_name, auth_header_value_encrypted, risk_tier)
                VALUES (%s, %s, %s, %s, %s, NULL, NULL, 'irreversible')
                RETURNING id
                """,
                (user["user_id"], body.agent_type, "odoo", mcp_url, "streamable-http"),
            )
            (row_id,) = cur.fetchone()
        conn.commit()
    except Exception as e:
        # P0: the instance was just created on Od-MCP but the row insert
        # failed -- revoke best-effort so no orphaned live token remains.
        _od_mcp_revoke_instance(instance_name)
        logger.error(f"odoo connect row insert failed for {user['user_id']}: {e}")
        raise HTTPException(status_code=503, detail="Tenant MCP server store unavailable")
    finally:
        conn.close()

    return _verify_and_persist(str(row_id), user["user_id"], body.agent_type, mcp_url, None, None)


@router.get("")
def list_servers(agent_type: Optional[str] = None, user: dict = Depends(get_current_user_payload)):
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            if agent_type:
                _check_agent_type(agent_type)
                cur.execute(
                    f"SELECT {_LIST_COLUMNS} FROM product.tenant_custom_mcp_servers"
                    " WHERE user_id = %s AND agent_type = %s AND status != 'disabled'"
                    " ORDER BY created_at DESC",
                    (user["user_id"], agent_type),
                )
            else:
                cur.execute(
                    f"SELECT {_LIST_COLUMNS} FROM product.tenant_custom_mcp_servers"
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
    _require_paid_tier(user["user_id"])
    _require_cerveau_engine(user["user_id"], agent_type)

    auth_header_value = None
    if encrypted is not None:
        try:
            auth_header_value = mcp_server_encryption.decrypt_auth_header_value(bytes(encrypted))
        except Exception as e:
            logger.error(f"tenant MCP server auth header decrypt failed for {server_id}: {e}")

    return _verify_and_persist(server_id, user["user_id"], agent_type, url, auth_header_name, auth_header_value)


class UpdateDisabledToolsRequest(BaseModel):
    disabled_tools: list[str] = Field(default_factory=list, max_length=500)


@router.patch("/{server_id}/tools")
def update_disabled_tools(server_id: str, body: UpdateDisabledToolsRequest, user: dict = Depends(get_current_user_payload)):
    """Per-tool allow/deny for one already-verified server (§B8) — the
    denylist Cerveau's `TenantCustomMcpServer::disabled_tools` applies at
    MCP-connect time so a disabled tool is never advertised to the model,
    mirroring `disabled_toolkits`' whole-toolkit denylist one level down.
    Requires the row's own `tools_json` (not the live server) as the
    source of truth for valid names — asking the tenant-supplied endpoint
    to validate its own tool names would be circular and adds a network
    round trip a config-only settings tab has no reason to take."""
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tools_json FROM product.tenant_custom_mcp_servers"
                " WHERE id = %s AND user_id = %s AND status != 'disabled'",
                (server_id, user["user_id"]),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Server not found")
            (tools_json,) = row
            known_names = {t["name"] for t in (tools_json or []) if isinstance(t, dict) and t.get("name")}
            unknown = [n for n in body.disabled_tools if n not in known_names]
            if unknown:
                raise HTTPException(status_code=400, detail=f"Unknown tool name(s): {', '.join(unknown[:5])}")

            cur.execute(
                f"""
                UPDATE product.tenant_custom_mcp_servers
                SET disabled_tools = %s, updated_at = now()
                WHERE id = %s AND user_id = %s
                RETURNING {_LIST_COLUMNS}
                """,
                (list(dict.fromkeys(body.disabled_tools)), server_id, user["user_id"]),
            )
            updated = cur.fetchone()
        conn.commit()
        return _row_to_public_dict(updated)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"tenant MCP server tool-toggle failed for {server_id}: {e}")
        raise HTTPException(status_code=503, detail="Tenant MCP server store unavailable")
    finally:
        conn.close()


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
                RETURNING id, agent_type, name
                """,
                (server_id, user["user_id"]),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            raise HTTPException(status_code=404, detail="Server not found or already disabled")
        # P0: revoke the tenant instance on the shared Od-MCP server so the
        # bearer token stops working. Fail-open: the backend row (source of
        # truth for Cerveau) is already disabled above.
        try:
            _row_id, _agent_type, _name = row
            if _name == "odoo":
                _od_mcp_revoke_instance(f"t_{user['user_id']}_{_agent_type}"[:64])
        except Exception as e:
            logger.warning(f"Od-MCP revoke after disable best-effort failed for {server_id}: {e}")
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
                "SELECT name, url, transport, auth_header_name, auth_header_value_encrypted, risk_tier, disabled_tools"
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
    for name, url, transport, auth_header_name, encrypted, risk_tier, disabled_tools in rows:
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
                "disabled_tools": list(disabled_tools) if disabled_tools else [],
            }
        )
    return {"servers": servers}

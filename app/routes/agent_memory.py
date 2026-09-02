"""Agent memory proxy — dashboard "White-Box Memory" → Cerveau gateway.

docs/CERVEAU-WORKING-OFFICE-PLANNING.md's White-Box Memory feature: lets a
user see and edit/delete what their agent remembers about them. Mirrors
agent_approvals.py's shape/conventions exactly — same two HA instances, same
X-Webhook-Secret + X-Tenant-Id/X-Agent-Type tenant-scoping contract, same
"enumerate this user's agent_types, try every (instance, agent_type)
combination" merge strategy — because the underlying gateway routes
(`GET/PUT/DELETE /webhook/memory[...]`, crates/zeroclaw-gateway/src/
api_tenant_memory.rs) follow the exact same two-layer auth pattern as
`/webhook/approvals` (patch documented there: `api_tenant_approvals.rs`).

One extra wrinkle memory has that approvals doesn't: every call also needs
an `agent=<host alias>` query param — a *separate* axis from X-Agent-Type,
naming which `[agents.<alias>]` config block (model provider + memory
backend) a tenant turn borrows on the gateway. It's a required param on the
gateway side, not optional, and gets it wrong silently resolves to a
different (or no) backend rather than erroring loudly at first glance — so
this is NOT guessed here. Live tenant turns with no explicit override
resolve to the gateway's alphabetically-first *enabled* `[agents.<alias>]`
entry (`resolved_runtime_agent_alias()`, crates/zeroclaw-config/src/
schema.rs:4625) when there's no literal `[agents.default]` — confirmed
against the live config.toml (aliases: analyst_brain, builder_brain,
comms_brain, diagnostic_brain, security_brain, workflow_brain; no `default`
entry) to resolve to `analyst_brain`, and independently corroborated by
[[zeroclaw-mcp-and-agent-routing]] memory's live-observed "/webhook IGNORES
the agent field, all copilot traffic = analyst_brain, not workflow_brain."
Kept configurable via env rather than only a literal, since this is a fact
about the *daemon's* config, not this proxy's — if that config ever changes,
ops shouldn't need a code change here to match it.

Security contract: identical to agent_approvals.py — JWT required on every
route, the dashboard user id comes exclusively from the verified JWT, and
the gateway's own tenant/key scoping (not this proxy) is the real
authorization boundary; this proxy only narrows what it even asks for.
"""

import logging
import os
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.routes.agent_actions import get_current_user_payload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent-memory", tags=["agent-memory"])

# Same two HA instances agent_approvals.py talks to — one daemon, one
# tenant model, both surfaces live on the same gateway.
_CERVEAU_BASES = [
    os.getenv("CERVEAU_APPROVAL_BASE_1", "http://host.docker.internal:3100"),
    os.getenv("CERVEAU_APPROVAL_BASE_2", "http://host.docker.internal:3101"),
]
_HOST_AGENT = os.getenv("CERVEAU_MEMORY_HOST_AGENT", "analyst_brain")
_TIMEOUT = httpx.Timeout(15.0)


def _webhook_secret() -> str:
    secret = os.getenv("CERVEAU_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="Cerveau webhook secret not configured")
    return secret


def _agent_types_for_user(user_id: str) -> List[str]:
    """Every agent_type this user has a real profile for — same query
    agent_approvals.py runs, duplicated rather than imported so this module
    stays self-contained (matches this codebase's existing convention —
    composio-connection-sync.py duplicates the identical query shape too)."""
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise HTTPException(status_code=503, detail="DATABASE_URL not set")
    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT agent_type FROM product.agent_profiles WHERE user_id = %s",
                (user_id,),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _require_user_id(user: dict) -> str:
    user_id = str(user.get("user_id") or user.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="No user id in session")
    return user_id


def _tenant_headers(user_id: str, agent_type: str) -> dict:
    return {
        "X-Webhook-Secret": _webhook_secret(),
        "X-Tenant-Id": user_id,
        "X-Agent-Type": agent_type,
    }


async def _fetch_memory(client: httpx.AsyncClient, base: str, user_id: str, agent_type: str) -> list:
    res = await client.get(
        f"{base}/webhook/memory",
        params={"agent": _HOST_AGENT},
        headers=_tenant_headers(user_id, agent_type),
    )
    if res.status_code in (401, 404):
        return []  # no rows / this tenant selector has none on this instance
    res.raise_for_status()
    return res.json().get("entries", [])


@router.get("")
async def list_memory(user: dict = Depends(get_current_user_payload)):
    """Every memory entry owned by the calling user, merged across every
    agent_type they have a profile for and both HA instances."""
    user_id = _require_user_id(user)

    agent_types = _agent_types_for_user(user_id)
    if not agent_types:
        return {"entries": []}

    out = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for base in _CERVEAU_BASES:
            for agent_type in agent_types:
                try:
                    rows = await _fetch_memory(client, base, user_id, agent_type)
                    for row in rows:
                        row["_gateway_base"] = base
                        row["_agent_type"] = agent_type
                    out.extend(rows)
                except Exception as e:  # noqa: BLE001 — one lookup failing must not hide the rest
                    logger.warning(
                        "memory list failed on %s for %s/%s: %s", base, user_id, agent_type, e
                    )
    return {"entries": out}


def _candidates(user_id: str, gateway_base: Optional[str], agent_type: Optional[str]):
    """Same hint-or-rescan shape agent_approvals.py's resolve endpoint
    uses: trust the list response's own hints when both are given together,
    otherwise fall back to trying every (base, agent_type) combination."""
    if gateway_base and agent_type:
        return [(gateway_base, agent_type)]
    return [(base, at) for base in _CERVEAU_BASES for at in _agent_types_for_user(user_id)]


class MemoryEditBody(BaseModel):
    content: str
    category: Optional[str] = None
    # Optional hints from the list response — skip the re-scan when present.
    gateway_base: Optional[str] = None
    agent_type: Optional[str] = None


@router.put("/{key}")
async def edit_memory(key: str, body: MemoryEditBody, user: dict = Depends(get_current_user_payload)):
    """Edit an existing entry's content (and, optionally, its category).
    The gateway 404s if `key` doesn't already exist for the tried tenant —
    this is an edit surface, not a way to plant new memories from the
    dashboard."""
    user_id = _require_user_id(user)
    payload = {"content": body.content}
    if body.category:
        payload["category"] = body.category

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for base, agent_type in _candidates(user_id, body.gateway_base, body.agent_type):
            try:
                res = await client.put(
                    f"{base}/webhook/memory/{key}",
                    params={"agent": _HOST_AGENT},
                    headers=_tenant_headers(user_id, agent_type),
                    json=payload,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("memory edit attempt failed on %s/%s: %s", base, agent_type, e)
                continue
            if res.status_code == 404:
                continue  # not this tenant's key on this instance/agent_type
            if res.status_code >= 400:
                raise HTTPException(
                    status_code=502, detail=f"Cerveau memory edit failed: {res.text[:300]}"
                )
            return res.json()

    raise HTTPException(status_code=404, detail="Memory entry not found, or not yours")


@router.delete("/{key}")
async def delete_memory(
    key: str,
    gateway_base: Optional[str] = None,
    agent_type: Optional[str] = None,
    user: dict = Depends(get_current_user_payload),
):
    """Delete one entry, scoped to the caller's own tenant. The gateway's
    own tenant-scoped `Memory::forget` (a SQL `WHERE key = ? AND agent_id =
    ?`) is the real authorization boundary — this proxy only narrows which
    (base, agent_type) combinations get tried."""
    user_id = _require_user_id(user)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for base, at in _candidates(user_id, gateway_base, agent_type):
            try:
                res = await client.delete(
                    f"{base}/webhook/memory/{key}",
                    params={"agent": _HOST_AGENT},
                    headers=_tenant_headers(user_id, at),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("memory delete attempt failed on %s/%s: %s", base, at, e)
                continue
            if res.status_code == 404:
                continue
            if res.status_code >= 400:
                raise HTTPException(
                    status_code=502, detail=f"Cerveau memory delete failed: {res.text[:300]}"
                )
            return res.json()

    raise HTTPException(status_code=404, detail="Memory entry not found, or not yours")

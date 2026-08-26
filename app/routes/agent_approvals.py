"""Agent approval proxy — dashboard → Cerveau gateway pending-approvals API.

docs/CERVEAU-APPROVAL-UX-PLAN.md Phase A + docs/CERVEAU-N8N-ORCHESTRATION-PLAN.md.
The gateway's `GET /webhook/approvals` and `POST /webhook/approvals/{id}/resolve`
are tenant-scoped (X-Webhook-Secret + X-Tenant-Id/X-Agent-Type, row-ownership
checked server-side) but not reachable with a bare user id alone — a tenant
selector on the gateway is `(user_id, agent_type)`, and one dashboard user can
have more than one agent_type profile (e.g. both `office_assistant` and
`autonomous`). This proxy enumerates the caller's known agent_types from
`product.agent_profiles` and queries every (agent_type x instance)
combination, merging the results.

Rewritten 2026-08-23 — the original draft of this file called a route
(`/api/approvals`) that was never actually registered on the gateway (the
real path is `/admin/approvals`, admin_reload_gate-authed, not
X-Webhook-Secret-authed) and used `:` as the tenant_id separator where the
gateway's own format is `user_id.agent_type` — both bugs, caught before this
route was ever exercised against a live instance. `GET /webhook/approvals`
(added the same day, `crates/zeroclaw-gateway/src/api_tenant_approvals.rs`)
is the real, tenant-scoped, already-tested fit for what this file needs.

Two HA instances (`:3100`/`:3101`) each hold their own pending-store
(separate `data_dir`s), so listing merges both and resolve tries both — the
row lives on whichever instance created it.

Security contract:
  - JWT required on every route (same dependency as the rest of /api/v1).
  - The dashboard user id comes exclusively from the verified JWT, never
    client payload.
  - Every gateway call carries X-Tenant-Id=user_id, X-Agent-Type=<one of the
    user's own agent_profiles rows> — the gateway's own row-ownership check
    is still the real authorization boundary; this proxy narrows what it
    even asks for.
"""

import logging
import os
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.routes.agent_actions import get_current_user_payload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent-approvals", tags=["agent-approvals"])

# Both HA instances share one binary but hold separate pending-stores.
# Cerveau (zeroclaw-cerveau/-b) runs as a native systemd process on the VPS
# host, bound to the host's own 127.0.0.1 — but this route runs inside the
# avry-backend Docker container, where 127.0.0.1 is the container's own
# loopback, not the host's (same class of gap fixed for n8n and Cerveau's own
# public bind elsewhere in this project). Confirmed live: from inside the
# container, 127.0.0.1:3100 is unreachable (connection refused) while
# host.docker.internal:3100 returns 200 — this silently broke both listing
# and resolving every pending approval (caught per-base, logged as a
# warning, never surfaced) until this default was fixed 2026-08-25.
_CERVEAU_BASES = [
    os.getenv("CERVEAU_APPROVAL_BASE_1", "http://host.docker.internal:3100"),
    os.getenv("CERVEAU_APPROVAL_BASE_2", "http://host.docker.internal:3101"),
]
_TIMEOUT = httpx.Timeout(10.0)


def _webhook_secret() -> str:
    secret = os.getenv("CERVEAU_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="Cerveau webhook secret not configured")
    return secret


def _agent_types_for_user(user_id: str) -> List[str]:
    """Every agent_type this user has a real profile for — the set of
    tenant selectors worth querying. Mirrors the exact query shape
    composio-connection-sync.py already uses on the VPS."""
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


async def _fetch_pending(client: httpx.AsyncClient, base: str, user_id: str, agent_type: str) -> list:
    res = await client.get(
        f"{base}/webhook/approvals",
        params={"status": "pending"},
        headers={
            "X-Webhook-Secret": _webhook_secret(),
            "X-Tenant-Id": user_id,
            "X-Agent-Type": agent_type,
        },
    )
    if res.status_code in (401, 404):
        return []  # no rows / this tenant selector has none on this instance
    res.raise_for_status()
    return res.json().get("approvals", [])


@router.get("")
async def list_pending_approvals(user: dict = Depends(get_current_user_payload)):
    """All pending approvals owned by the calling user, merged across every
    agent_type they have a profile for and both HA instances."""
    user_id = str(user.get("user_id") or user.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="No user id in session")

    agent_types = _agent_types_for_user(user_id)
    if not agent_types:
        return {"approvals": []}

    out = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for base in _CERVEAU_BASES:
            for agent_type in agent_types:
                try:
                    rows = await _fetch_pending(client, base, user_id, agent_type)
                    for row in rows:
                        row["_gateway_base"] = base
                        row["_agent_type"] = agent_type
                    out.extend(rows)
                except Exception as e:  # noqa: BLE001 — one lookup failing must not hide the rest
                    logger.warning(
                        "pending-approval list failed on %s for %s/%s: %s",
                        base, user_id, agent_type, e,
                    )
    return {"approvals": out}


class ApprovalDecision(BaseModel):
    decision: str  # "approve" | "deny"
    # Optional hints from the list response — skips the re-scan below when
    # present. Both must be given together to be trusted; either missing
    # falls back to searching every (base, agent_type) combination.
    gateway_base: Optional[str] = None
    agent_type: Optional[str] = None


async def _resolve_on(client: httpx.AsyncClient, base: str, approval_id: str,
                       user_id: str, agent_type: str, decision: str) -> Optional[dict]:
    headers = {
        "X-Webhook-Secret": _webhook_secret(),
        "X-Tenant-Id": user_id,
        "X-Agent-Type": agent_type,
    }
    res = await client.post(
        f"{base}/webhook/approvals/{approval_id}/resolve",
        headers=headers,
        json={"decision": decision},
    )
    if res.status_code == 404:
        return None  # row not on this instance/agent_type, or not this tenant's
    if res.status_code >= 400:
        # httpx.Response has no .ok (that's a `requests` attribute) -- the
        # original `if not res.ok` raised AttributeError on every call here,
        # which the caller's `except Exception: continue` swallowed. Net
        # effect: the resolve against Cerveau succeeded every time (real
        # state change, confirmed live), but this route always reported a
        # false "Approval not found" back to whoever called it -- the
        # dashboard Approvals page and the console's Approve/Deny buttons
        # included. Found and fixed 2026-08-25.
        detail = res.text[:300]
        raise HTTPException(status_code=502, detail=f"Cerveau resolve failed: {detail}")
    return res.json()


@router.post("/{approval_id}/resolve")
async def resolve_approval(approval_id: str, body: ApprovalDecision,
                            user: dict = Depends(get_current_user_payload)):
    """Approve/deny one pending row. The gateway's own row-ownership check
    (X-Tenant-Id must match the row's stored tenant_id) is the real
    authorization boundary — this proxy only narrows which (base, agent_type)
    combinations get tried."""
    if body.decision not in ("approve", "deny"):
        raise HTTPException(status_code=400, detail='decision must be "approve" or "deny"')
    user_id = str(user.get("user_id") or user.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="No user id in session")

    candidates = []
    if body.gateway_base and body.agent_type:
        candidates = [(body.gateway_base, body.agent_type)]
    else:
        agent_types = _agent_types_for_user(user_id)
        candidates = [(base, at) for base in _CERVEAU_BASES for at in agent_types]

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        for base, agent_type in candidates:
            try:
                result = await _resolve_on(client, base, approval_id, user_id, agent_type, body.decision)
            except HTTPException:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("resolve attempt failed on %s/%s: %s", base, agent_type, e)
                continue
            if result is not None:
                return {"success": True, "outcome": result.get("outcome"), "reply": result.get("reply_text")}

    raise HTTPException(status_code=404, detail="Approval not found, not yours, or already resolved")

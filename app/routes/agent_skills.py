"""Agent Skills proxy — dashboard -> Cerveau gateway tenant Skills listing.

ADR-008 Phase 4's "read-only Skills listing per agent". Mirrors
agent_approvals.py's shape (same JWT-in/webhook-secret-out proxy, same
Docker-networking base URLs), simplified where the underlying data allows
it, and reuses agent_approvals.py's helpers directly rather than risking a
second, subtly different copy of infrastructure that already had one real
bug (the host.docker.internal gap that silently broke every pending-
approval call until 2026-08-25 — see that module's own docstring).

Two differences from agent_approvals.py, both because a skill set is not
per-tenant runtime state the way a pending approval is:

- **One instance queried, not both merged.** Skills are resolved from the
  install's own config/skills directory — identical across :3100 and :3101
  (ADR-011's hourly drift check keeps them that way) — not read from a
  per-instance store. The second base is a fallback on a transport failure,
  never a second source of different rows to merge.
- **One agent_type queried, not every one the user has.** Cerveau's
  `/webhook/skills` resolves the same runtime alias for every agent_type
  today (this install has no per-agent_type Cerveau alias at all — see
  crates/zeroclaw-gateway/src/api_tenant_skills.rs's own doc), so there is
  nothing to merge across agent_type either. The caller still names one,
  and it is still checked against the caller's own profiles, because the
  gateway's tenant-selector auth needs a real, attributable identity, not
  because a different agent_type would return different skills.

Security contract identical to agent_approvals.py: JWT required, user id
from the verified token only, and the requested agent_type must be one the
caller actually has a profile for — never trusted from the query string
alone.
"""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.routes.agent_actions import get_current_user_payload
from app.routes.agent_approvals import _CERVEAU_BASES, _agent_types_for_user, _webhook_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent-skills", tags=["agent-skills"])

_TIMEOUT = httpx.Timeout(10.0)


@router.get("")
async def list_agent_skills(agent_type: str, user: dict = Depends(get_current_user_payload)):
    """The calling user's agent's effective skill set, read-only."""
    user_id = str(user.get("user_id") or user.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="No user id in session")

    if agent_type not in _agent_types_for_user(user_id):
        raise HTTPException(status_code=403, detail="No agent profile for that agent_type")

    headers = {
        "X-Webhook-Secret": _webhook_secret(),
        "X-Tenant-Id": user_id,
        "X-Agent-Type": agent_type,
    }
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for base in _CERVEAU_BASES:
            try:
                res = await client.get(f"{base}/webhook/skills", headers=headers)
                res.raise_for_status()
                return {"skills": res.json().get("skills", [])}
            except Exception as e:  # noqa: BLE001 — one base failing must not hide the fallback
                logger.warning(
                    "skills list failed on %s for %s/%s: %s", base, user_id, agent_type, e
                )
                last_error = e
    # Both instances failed — fail closed rather than returning an empty
    # list, which would read as "this agent has no skills" rather than
    # "the request could not be made at all".
    raise HTTPException(status_code=503, detail="Skills service unavailable") from last_error

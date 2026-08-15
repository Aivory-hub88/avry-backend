"""
Per-agent, per-toolkit tool scope — lets an operator turn off one of the
*external* (Composio-sourced) toolkits their agent would otherwise get, e.g.
disable Zendesk for their `customer_service` agent without losing the
zero-signup native-bridge/OfficeCLI tools that are always on.

Deliberately scoped to Composio-sourced toolkits only. Native-bridge,
OfficeCLI, Lightpanda, and pdf-oxide are unconditional per the 2026-08-08
"agnostic tools" product direction — they are not represented here at all,
so there is no row/toggle that could accidentally leave a tenant's agent
with zero tools.

Table: product.agent_tool_scope (avry-postgres).

Default when a row is absent: **enabled**. Every connected external toolkit
is granted unconditionally today; this table must only ever be able to
narrow that, never silently regress a tenant who has never touched the new
Tools tab back to "nothing".

Dashboard-facing (JWT auth):
    GET /api/v1/agent-profiles/{agent_type}/tools   -> {toolkit_slug: enabled}
    PUT /api/v1/agent-profiles/{agent_type}/tools   -> upsert one or more toggles

Cerveau reads this table directly (its own short-TTL-cached resolver, same
shape as patch 0024's ToolkitConnectionResolver) — there is no internal HTTP
route here, unlike agent_profiles.py's /internal endpoint.
"""

import logging
import os
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.routes.agent_actions import get_current_user_payload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent-profiles", tags=["agent-tool-scope"])

# The only toolkit slugs an operator can toggle, per agent type — matches
# Cerveau's live `requires_composio_toolkit` config exactly (see
# docs/CERVEAU-STATUS.md §4 patch 0024). Stripe is retired (2026-08-08
# product direction), not listed. Update this alongside any future
# Composio-sourced toolkit wiring in Cerveau's config.toml.
TOGGLEABLE_TOOLKITS: Dict[str, list] = {
    "customer_service": ["zendesk", "hubspot", "slack"],
    "leads_qualifier": ["hubspot", "slack"],
    "office_assistant": ["slack", "asana"],
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS product.agent_tool_scope (
    user_id      TEXT NOT NULL,
    agent_type   TEXT NOT NULL,
    toolkit_slug TEXT NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT true,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, agent_type, toolkit_slug)
);
"""

_schema_ready = False


def _connect():
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set — agent tool scope requires Postgres")
    return psycopg2.connect(dsn, connect_timeout=5)


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_SQL)
    conn.commit()
    _schema_ready = True


def _check_agent_type(agent_type: str) -> list:
    """Returns the agent type's toggleable slugs, or 404s if there are
    none — an agent type with no external toolkits has nothing to scope,
    same "unknown/inapplicable" signal as agent_profiles.py's check."""
    slugs = TOGGLEABLE_TOOLKITS.get(agent_type)
    if not slugs:
        raise HTTPException(
            status_code=404,
            detail=f"'{agent_type}' has no toggleable external toolkits",
        )
    return slugs


class ToolScopeUpdate(BaseModel):
    # toolkit_slug -> enabled. Unknown slugs for this agent_type are
    # rejected below rather than silently accepted and never read by
    # anything — a typo'd slug should fail loudly, not toggle nothing.
    toolkits: Dict[str, bool]


@router.get("/{agent_type}/tools")
def get_tool_scope(agent_type: str, user: dict = Depends(get_current_user_payload)):
    known_slugs = _check_agent_type(agent_type)
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT toolkit_slug, enabled FROM product.agent_tool_scope"
                " WHERE user_id = %s AND agent_type = %s",
                (user["user_id"], agent_type),
            )
            rows = dict(cur.fetchall())
        # Default-enabled for any known slug with no row yet.
        tools = {slug: rows.get(slug, True) for slug in known_slugs}
        return {"agent_type": agent_type, "tools": tools}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"tool scope lookup failed for {user['user_id']}/{agent_type}: {e}")
        raise HTTPException(status_code=503, detail="Tool scope store unavailable")
    finally:
        conn.close()


@router.put("/{agent_type}/tools")
def put_tool_scope(agent_type: str, body: ToolScopeUpdate, user: dict = Depends(get_current_user_payload)):
    known_slugs = _check_agent_type(agent_type)
    unknown = set(body.toolkits) - set(known_slugs)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown toolkit(s) for '{agent_type}': {sorted(unknown)}",
        )
    if not body.toolkits:
        return {"ok": True, "agent_type": agent_type, "tools": {}}

    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            for slug, enabled in body.toolkits.items():
                cur.execute(
                    """
                    INSERT INTO product.agent_tool_scope (user_id, agent_type, toolkit_slug, enabled, updated_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (user_id, agent_type, toolkit_slug)
                    DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()
                    """,
                    (user["user_id"], agent_type, slug, enabled),
                )
        conn.commit()
        logger.info(f"Tool scope saved: {agent_type} for {user['user_id']}: {body.toolkits}")
        return {"ok": True, "agent_type": agent_type, "tools": body.toolkits}
    except Exception as e:
        logger.error(f"tool scope save failed: {e}")
        raise HTTPException(status_code=503, detail="Tool scope store unavailable")
    finally:
        conn.close()

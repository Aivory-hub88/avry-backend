"""
Active agents — which of a tenant's agents are "active" (ADR-020).

An agent is active for a tenant when it is deployed on a channel: a Telegram
binding, a Slack installation, or a non-revoked agent API key. (Room/Team
selection joins in a later phase, ADR-020 P3.) The vps-bridge reads this per
turn and forwards it to Cerveau as `X-Active-Agents`, so an agent only ever
sees teammates the user actually deployed.

Internal only (shared-secret token, same as agent-profiles/internal): this is
tenant data keyed by user_id, called service-to-service by the bridge. Every
source is fail-open — a source that errors contributes nothing rather than
failing the turn, matching how the bridge treats this lookup (an empty result
means "no filtering", i.e. today's behaviour).
"""

import logging

from fastapi import APIRouter, Depends

from app.database.db_service import DatabaseService
from app.routes import agent_api_keys
from app.routes.agent_actions import require_internal_token
from app.routes.agent_roster import AGENT_ROSTER
from app.services.slack_service import SlackService
from app.services.telegram_service import TelegramService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/active-agents", tags=["active-agents"])

db_service = DatabaseService()
telegram_service = TelegramService(db_service)
slack_service = SlackService(db_service)

AGENT_TYPES = {a["agent_type"] for a in AGENT_ROSTER}


def merge_sources(by_source: dict) -> list:
    """{source: [agent_type, ...]} -> [{agent_type, sources}], roster order.

    Unknown agent types are dropped (the roster is the allow-list), sources
    are de-duplicated and sorted so the output is stable.
    """
    found: dict = {}
    for source, agent_types in by_source.items():
        for agent_type in agent_types or []:
            if agent_type in AGENT_TYPES:
                found.setdefault(agent_type, set()).add(source)
    return [
        {"agent_type": a["agent_type"], "sources": sorted(found[a["agent_type"]])}
        for a in AGENT_ROSTER
        if a["agent_type"] in found
    ]


def _telegram_agents(user_id: str) -> list:
    return [b.get("agent_type") for b in telegram_service.list_bindings(user_id)]


def _slack_agents(user_id: str) -> list:
    return [i.get("agent_type") for i in slack_service.list_installations(user_id)]


def _api_key_agents(user_id: str) -> list:
    conn = agent_api_keys._connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT agent_type FROM product.agent_api_keys"
                " WHERE user_id = %s AND status = 'active'",
                (user_id,),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


_SOURCES = {
    "telegram": _telegram_agents,
    "slack": _slack_agents,
    "api_key": _api_key_agents,
}


def collect_active_agents(user_id: str) -> list:
    by_source: dict = {}
    for name, fetch in _SOURCES.items():
        try:
            by_source[name] = fetch(user_id)
        except Exception as e:  # fail-open per source
            logger.warning(f"active-agents: {name} lookup failed for {user_id}: {e}")
    return merge_sources(by_source)


@router.get("/internal/{user_id}", dependencies=[Depends(require_internal_token)])
def internal_get(user_id: str):
    return {"agents": collect_active_agents(user_id)}

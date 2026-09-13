"""
Live "is this agent doing something right now" tracking.

Backs the dashboard's Console/Notification-Center "Running now" section,
which previously had no data source at all and rendered a permanently
static "Not running anything right now." (see docs/CERVEAU-WORKING-OFFICE-
PLANNING.md) — there was nowhere in the stack that recorded a turn as
in-flight, only completed actions (agent_actions) and schedule status
(tenant_scheduled_runs), neither of which says "this is happening right
this second."

`_route_to_agent` in telegram_service.py is the one choke point every live
turn — Console chat, Telegram messages, Slack messages — already passes
through before calling out to the agent gateway, so it's the correct place
to mark start/end rather than adding a new call site per channel.

Uses the existing `cache_manager` (Redis, already wired for auth/rate-limit/
subscription caching — app/utils/cache.py) rather than a new dependency.
Fails open the same way every other cache_manager caller does: a Redis
outage means "Running now" quietly shows nothing rather than the turn
itself failing, which is the right trade-off for a status indicator.

TTL is the safety net, not the normal cleanup path — normal cleanup is the
`finally` block in _route_to_agent. It exists for the case a worker is
killed mid-request (OOM, deploy, unhandled crash) and never reaches that
`finally`; without it, a single crashed turn would show as "running"
forever. Set comfortably above _route_to_agent's own 195s gateway timeout.
"""

from datetime import datetime, timezone
from typing import Optional

from app.utils.cache import cache_manager

_TTL_SECONDS = 240
_KEY_PREFIX = "agent_running"


def _key(user_id: str, agent_type: str) -> str:
    return f"{_KEY_PREFIX}:{user_id}:{agent_type}"


def mark_running(user_id: str, agent_type: str, channel: str) -> None:
    cache_manager.set(
        _key(user_id, agent_type),
        {"started_at": datetime.now(timezone.utc).isoformat(), "channel": channel},
        ttl=_TTL_SECONDS,
    )


def clear_running(user_id: str, agent_type: str) -> None:
    cache_manager.delete(_key(user_id, agent_type))


def list_running(user_id: str) -> list[dict]:
    """Every agent_type currently marked running for this user, each as
    {"agent_type", "started_at", "channel"}. Order is not meaningful."""
    out: list[dict] = []
    try:
        keys = cache_manager.redis.keys(f"{_KEY_PREFIX}:{user_id}:*")
    except Exception:
        return out
    for key in keys:
        entry = cache_manager.get(key)
        if not isinstance(entry, dict):
            continue
        agent_type = key.split(":")[-1]
        out.append({
            "agent_type": agent_type,
            "started_at": entry.get("started_at"),
            "channel": entry.get("channel"),
        })
    return out

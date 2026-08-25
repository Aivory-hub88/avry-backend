"""
Wipes a demo/test account's usage data — Deep Diagnostic, Blueprint, Roadmap
and Workflow Copilot results — so the account can be handed to the next
prospect without carrying over a previous demo's content, and force-logs it
out everywhere.

The usage-data tables belong to avry-user-dashboard's own Postgres schema
(`dashboard.*`, its migrations/dashboard-storage.sql) rather than anything
this service otherwise owns, but every Aivory service shares one
DATABASE_URL/database — only the schema differs — so a schema-qualified
DELETE reaches them directly, no cross-service HTTP call needed.

Deliberately does NOT touch billing.* (credits, payment history) — those are
financial records, not "demo content", and clearing them isn't part of what
was asked for.
"""

import logging
from typing import Dict

from app.database import pg_service as pg

logger = logging.getLogger(__name__)

USAGE_DATA_TABLES = (
    "dashboard.diagnostic_contexts",
    "dashboard.diagnostic_results",
    "dashboard.blueprints",
    "dashboard.roadmaps",
    "dashboard.diagnostic_history",
    "dashboard.workflow_versions",
    "dashboard.workflow_fixtures",
    "dashboard.workflow_approval_cases",
    "dashboard.n8n_credentials",
)


async def clear_usage_data(user_id: str) -> Dict[str, int]:
    """Delete every dashboard.* row for user_id. Returns rows-deleted per table."""
    pool = await pg.get_pool()
    deleted: Dict[str, int] = {}
    for table in USAGE_DATA_TABLES:
        try:
            result = await pool.execute(f"DELETE FROM {table} WHERE user_id = $1", user_id)
            # asyncpg's execute() returns a command tag like "DELETE 3".
            deleted[table] = int(result.split()[-1])
        except Exception as e:
            logger.warning("Could not clear %s for %s: %s", table, user_id, e)
            deleted[table] = -1
    logger.info("Cleared usage data for %s: %s", user_id, deleted)
    return deleted


async def invalidate_sessions(user_id: str) -> int:
    """Force-logout: delete every active session for user_id, everywhere."""
    pool = await pg.get_pool()
    result = await pool.execute("DELETE FROM sessions WHERE user_id = $1", user_id)
    return int(result.split()[-1])

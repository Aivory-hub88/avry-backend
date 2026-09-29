"""
Acting-as for Workspace agent turns (ADR-020 §1.1).

Agents in a Workspace belong to its leader (the space's owner). When a member
asks one, the turn must run in the leader's tenant (leader's memory, tier and
credits) with the member recorded only as the requester. The dashboard sends
the member's own JWT plus `acting_as`; this module decides whether that is
legitimate, against the dashboard tables, so a member can never name an
arbitrary user:

  - `acting_as` must be the OWNER of the space (its doc row), and
  - the requester must be able to WRITE in it: the owner themselves, a doc ACL
    `editor`/`owner`, or a workspace member with role `owner`/`editor`
    (same precedence as the dashboard's getDocRole).

Fail closed: any lookup error, missing/ownerless doc, or unknown role denies.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

WRITE_ROLES = {"owner", "editor"}


def decide(
    *,
    requested_by: str,
    acting_as: str,
    doc_owner: Optional[str],
    acl_role: Optional[str],
    member_role: Optional[str],
) -> bool:
    """Pure decision; see module docstring."""
    if not requested_by or not acting_as or not doc_owner:
        return False
    if doc_owner != acting_as:
        return False
    if requested_by == doc_owner:
        return True
    if acl_role is not None:
        return acl_role in WRITE_ROLES
    return member_role in WRITE_ROLES


def _connect():
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(dsn, connect_timeout=5)


def fetch_space_facts(space_id: str, requested_by: str) -> dict:
    """{doc_owner, acl_role, member_role} for a space, from the dashboard schema."""
    bare = space_id.removeprefix("workspace:").removeprefix("db:")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, owner, workspace_id FROM dashboard.workspace_docs"
                " WHERE id = ANY(%s)",
                ([bare, f"workspace:{bare}"],),
            )
            rows = cur.fetchall()
            # Prefer the room-keyed row (mirrors the dashboard's getDocRole).
            row = next((r for r in rows if str(r[0]).startswith("workspace:")), None) or (
                rows[0] if rows else None
            )
            if not row:
                return {"doc_owner": None, "acl_role": None, "member_role": None}
            owner, workspace_id = row[1], (row[2] or "default")
            cur.execute(
                "SELECT role FROM dashboard.workspace_doc_acl"
                " WHERE doc_id = %s AND user_id = %s",
                (bare, requested_by),
            )
            acl = cur.fetchone()
            cur.execute(
                "SELECT role FROM dashboard.workspace_members"
                " WHERE workspace_id = %s AND user_id = %s",
                (workspace_id, requested_by),
            )
            mem = cur.fetchone()
            return {
                "doc_owner": owner,
                "acl_role": acl[0] if acl else None,
                "member_role": mem[0] if mem else None,
            }
    finally:
        conn.close()


def can_act_as(space_id: str, requested_by: str, acting_as: str) -> bool:
    try:
        facts = fetch_space_facts(space_id, requested_by)
    except Exception as e:  # fail closed
        logger.warning(f"acting_as lookup failed for space {space_id}: {e}")
        return False
    return decide(requested_by=requested_by, acting_as=acting_as, **facts)

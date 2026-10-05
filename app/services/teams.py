"""
Teams (ADR-020 §3.3): a named set of one user's agents, attachable to channels.

A Team belongs to exactly one user (its leader). When a turn arrives on a
channel bound to a Team and the Team is `isolated` (default), the agents in
that turn may delegate only within the Team's members; the resolved list rides
to the bridge as `active_agents` (-> `X-Active-Agents`, ADR-020 P2).

Tables live in the `product` schema, created on first use like agent_api_keys.
Channel kinds supported so far: `workspace` (ref = space id), `telegram`
(ref = binding id). Console Rooms pass an explicit `team_id` instead of a
channel row. Slack, WhatsApp, Odoo and API keys are later phases.
"""

import logging
import os
import secrets
from typing import Optional

logger = logging.getLogger(__name__)

CHANNEL_KINDS = ("workspace", "telegram")
MAX_TEAMS_PER_USER = 20
MAX_MEMBERS = 12
MAX_CHANNELS_PER_TEAM = 50

_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS product;
CREATE TABLE IF NOT EXISTS product.teams (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    isolated   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS teams_user_idx ON product.teams (user_id);
CREATE TABLE IF NOT EXISTS product.team_members (
    team_id    TEXT NOT NULL REFERENCES product.teams(id) ON DELETE CASCADE,
    agent_type TEXT NOT NULL,
    PRIMARY KEY (team_id, agent_type)
);
CREATE TABLE IF NOT EXISTS product.team_channels (
    team_id TEXT NOT NULL REFERENCES product.teams(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    kind    TEXT NOT NULL,
    ref     TEXT NOT NULL,
    PRIMARY KEY (team_id, kind, ref)
);
-- one channel belongs to at most one of a user's teams
CREATE UNIQUE INDEX IF NOT EXISTS team_channels_user_channel_idx
    ON product.team_channels (user_id, kind, ref);
"""

_schema_ready = False


def _connect():
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set — Teams require Postgres")
    return psycopg2.connect(dsn, connect_timeout=5)


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_SQL)
    conn.commit()
    _schema_ready = True


def new_team_id() -> str:
    return "team_" + secrets.token_hex(8)


def active_agents_for_team(team: Optional[dict]) -> Optional[list]:
    """Agent list a turn on this Team's channel may delegate within.

    Only an `isolated` Team narrows delegation; a non-isolated (or missing)
    Team returns None = "no filter", i.e. the tenant-wide active set (P2).
    """
    if not team or not team.get("isolated", True):
        return None
    members = [m for m in team.get("agent_types", []) if m]
    return members or None


def _team_row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "name": row[1],
        "isolated": bool(row[2]),
        "created_at": row[3].isoformat() if row[3] else None,
    }


def list_teams(user_id: str) -> list:
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, isolated, created_at FROM product.teams"
                " WHERE user_id = %s ORDER BY created_at",
                (user_id,),
            )
            teams = [_team_row_to_dict(r) for r in cur.fetchall()]
            ids = [t["id"] for t in teams]
            members: dict = {i: [] for i in ids}
            channels: dict = {i: [] for i in ids}
            if ids:
                cur.execute(
                    "SELECT team_id, agent_type FROM product.team_members"
                    " WHERE team_id = ANY(%s) ORDER BY agent_type",
                    (ids,),
                )
                for team_id, agent_type in cur.fetchall():
                    members[team_id].append(agent_type)
                cur.execute(
                    "SELECT team_id, kind, ref FROM product.team_channels"
                    " WHERE team_id = ANY(%s) ORDER BY kind, ref",
                    (ids,),
                )
                for team_id, kind, ref in cur.fetchall():
                    channels[team_id].append({"kind": kind, "ref": ref})
            for t in teams:
                t["agent_types"] = members[t["id"]]
                t["channels"] = channels[t["id"]]
            return teams
    finally:
        conn.close()


def get_team(user_id: str, team_id: str) -> Optional[dict]:
    return next((t for t in list_teams(user_id) if t["id"] == team_id), None)


def create_team(user_id: str, name: str, agent_types: list, isolated: bool = True) -> dict:
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM product.teams WHERE user_id = %s", (user_id,))
            if cur.fetchone()[0] >= MAX_TEAMS_PER_USER:
                raise ValueError(f"At most {MAX_TEAMS_PER_USER} teams per account")
            team_id = new_team_id()
            cur.execute(
                "INSERT INTO product.teams (id, user_id, name, isolated) VALUES (%s, %s, %s, %s)",
                (team_id, user_id, name, isolated),
            )
            for agent_type in agent_types:
                cur.execute(
                    "INSERT INTO product.team_members (team_id, agent_type) VALUES (%s, %s)",
                    (team_id, agent_type),
                )
        conn.commit()
    finally:
        conn.close()
    return get_team(user_id, team_id)


def update_team(
    user_id: str,
    team_id: str,
    name: Optional[str] = None,
    agent_types: Optional[list] = None,
    isolated: Optional[bool] = None,
) -> Optional[dict]:
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM product.teams WHERE id = %s AND user_id = %s", (team_id, user_id)
            )
            if not cur.fetchone():
                return None
            if name is not None:
                cur.execute(
                    "UPDATE product.teams SET name = %s, updated_at = now() WHERE id = %s",
                    (name, team_id),
                )
            if isolated is not None:
                cur.execute(
                    "UPDATE product.teams SET isolated = %s, updated_at = now() WHERE id = %s",
                    (isolated, team_id),
                )
            if agent_types is not None:
                cur.execute("DELETE FROM product.team_members WHERE team_id = %s", (team_id,))
                for agent_type in agent_types:
                    cur.execute(
                        "INSERT INTO product.team_members (team_id, agent_type) VALUES (%s, %s)",
                        (team_id, agent_type),
                    )
        conn.commit()
    finally:
        conn.close()
    return get_team(user_id, team_id)


def delete_team(user_id: str, team_id: str) -> bool:
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM product.teams WHERE id = %s AND user_id = %s", (team_id, user_id)
            )
            deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    finally:
        conn.close()


def attach_channel(user_id: str, team_id: str, kind: str, ref: str) -> Optional[dict]:
    """Bind a channel to a Team. Re-binding a channel moves it (one Team per channel)."""
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM product.teams WHERE id = %s AND user_id = %s", (team_id, user_id)
            )
            if not cur.fetchone():
                return None
            cur.execute(
                "SELECT count(*) FROM product.team_channels WHERE team_id = %s", (team_id,)
            )
            if cur.fetchone()[0] >= MAX_CHANNELS_PER_TEAM:
                raise ValueError(f"At most {MAX_CHANNELS_PER_TEAM} channels per team")
            cur.execute(
                "DELETE FROM product.team_channels WHERE user_id = %s AND kind = %s AND ref = %s",
                (user_id, kind, ref),
            )
            cur.execute(
                "INSERT INTO product.team_channels (team_id, user_id, kind, ref) VALUES (%s, %s, %s, %s)",
                (team_id, user_id, kind, ref),
            )
        conn.commit()
    finally:
        conn.close()
    return get_team(user_id, team_id)


def detach_channel(user_id: str, team_id: str, kind: str, ref: str) -> Optional[dict]:
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM product.team_channels"
                " WHERE team_id = %s AND user_id = %s AND kind = %s AND ref = %s",
                (team_id, user_id, kind, ref),
            )
        conn.commit()
    finally:
        conn.close()
    return get_team(user_id, team_id)


def team_for_channel(user_id: str, kind: str, ref: str) -> Optional[dict]:
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT team_id FROM product.team_channels"
                " WHERE user_id = %s AND kind = %s AND ref = %s",
                (user_id, kind, ref),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return get_team(user_id, row[0]) if row else None


def resolve_active_agents(
    user_id: str,
    *,
    team_id: Optional[str] = None,
    kind: Optional[str] = None,
    ref: Optional[str] = None,
) -> Optional[list]:
    """Team-scoped active agents for a turn, or None (= tenant-wide).

    Fail-open: any error yields None, so a Teams outage never blocks a turn
    or narrows delegation by accident.
    """
    try:
        if team_id:
            team = get_team(user_id, team_id)
        elif kind and ref:
            team = team_for_channel(user_id, kind, ref)
        else:
            return None
        return active_agents_for_team(team)
    except Exception as e:
        logger.warning(f"team resolution failed for {user_id}: {e}")
        return None

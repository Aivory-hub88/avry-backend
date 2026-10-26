"""
Server-side firing of Team Space agent reminders (ADR-021 §4.1).

An agent's `team_remind` (and a `team_wait` with `until`) is stored by the
dashboard in dashboard.workspace_agent_reminders. Before this sweeper a due
reminder fired only while a member with write access had the room open,
because every agent turn runs under a member's JWT and a timer has none.

Each reminder row now records `credential_owner`: the member whose turn
created it. Every minute this sweeper:

  1. takes a Postgres advisory lock, so one worker sweeps at a time;
  2. lists (space_id, credential_owner) pairs with a due, unfired reminder;
  3. mints a short-lived access token for that member (15 min, `via:
     room-reminder`), never stored or logged;
  4. calls the dashboard's POST /api/workspace/{space}/reminders/fire with it.

The dashboard re-checks the member's write access to the room and runs the
same code the room poll runs, so the turn goes through the existing
/api/v1/telegram/discussion-turn with that member's tenant, credits and
approvals. When the member is gone, inactive, or no longer allowed in the
room, their reminders are handed back to the room poll (credential_owner set
to NULL) instead of being retried every minute.

Off unless ROOM_REMINDER_SWEEPER_ENABLED=true and DASHBOARD_INTERNAL_URL are
set; the room poll keeps firing reminders either way.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import jwt
import requests

from app.config import settings
from app.services.jwt_secret import JWT_SECRET

logger = logging.getLogger(__name__)

#: pg_try_advisory_lock key; any constant unique to this sweeper.
_LOCK_KEY = 7_021_004_001
#: Pairs handled per tick; the dashboard fires at most 3 reminders per call.
_BATCH = 20
#: Long enough for the fired turn and the chain it starts in the common case.
TOKEN_TTL_MINUTES = 15
_FIRE_TIMEOUT_SECONDS = 30


def _connect():
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set")
    conn = psycopg2.connect(dsn, connect_timeout=5)
    # Autocommit: the advisory lock is per session, and a failed statement
    # must not leave the unlock stuck in an aborted transaction.
    conn.autocommit = True
    return conn


def mint_room_token(user: dict, now: Optional[datetime] = None) -> str:
    """A short-lived access token for `user`, for one reminder sweep.

    Same claims the login token carries, so the dashboard and the backend
    verify it unchanged; `via` marks where it came from in any audit."""
    now = now or datetime.now(timezone.utc)
    payload = {
        "user_id": user["user_id"],
        "email": user.get("email"),
        "account_type": user.get("account_type") or "free",
        "type": "access",
        "via": "room-reminder",
        "iat": now,
        "exp": now + timedelta(minutes=TOKEN_TTL_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def due_pairs(cur) -> list[tuple[str, str]]:
    """(space_id, credential_owner) with a due, unfired reminder, oldest first."""
    cur.execute(
        """
        SELECT space_id, credential_owner
        FROM dashboard.workspace_agent_reminders
        WHERE fired_at IS NULL AND fire_at <= now() AND credential_owner IS NOT NULL
        GROUP BY space_id, credential_owner
        ORDER BY min(fire_at) ASC
        LIMIT %s
        """,
        (_BATCH,),
    )
    return [(str(r[0]), str(r[1])) for r in cur.fetchall()]


def load_owner(cur, user_id: str) -> Optional[dict]:
    """The member a token is minted for, or None when gone or inactive."""
    cur.execute(
        "SELECT id, email, account_type, is_active FROM users WHERE id = %s",
        (user_id,),
    )
    row = cur.fetchone()
    if not row or row[3] is False:
        return None
    return {"user_id": str(row[0]), "email": row[1], "account_type": row[2]}


def hand_back_to_room_poll(cur, space_id: str, owner: str) -> None:
    """Stop sweeping this owner's due reminders in this room; the room poll still fires them."""
    cur.execute(
        """
        UPDATE dashboard.workspace_agent_reminders SET credential_owner = NULL
        WHERE space_id = %s AND credential_owner = %s AND fired_at IS NULL AND fire_at <= now()
        """,
        (space_id, owner),
    )


def fire(base_url: str, space_id: str, token: str) -> int:
    """Ask the dashboard to fire this member's due reminders in one room. Returns the HTTP status."""
    url = f"{base_url.rstrip('/')}/api/workspace/{quote(space_id, safe='')}/reminders/fire"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=_FIRE_TIMEOUT_SECONDS,
    )
    return resp.status_code


def sweep_once(conn, base_url: str, fire_fn=fire) -> int:
    """One sweep under the advisory lock. Returns how many rooms were fired."""
    fired = 0
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_KEY,))
        if not cur.fetchone()[0]:
            return 0
        try:
            for space_id, owner in due_pairs(cur):
                user = load_owner(cur, owner)
                if user is None:
                    hand_back_to_room_poll(cur, space_id, owner)
                    continue
                try:
                    status = fire_fn(base_url, space_id, mint_room_token(user))
                except requests.RequestException as e:
                    # Dashboard unreachable: try again next tick, the room poll may fire first.
                    logger.warning("room reminder fire failed for space %s: %s", space_id, e)
                    break
                if status in (401, 403, 404):
                    # The member can no longer write in this room (or it is gone).
                    hand_back_to_room_poll(cur, space_id, owner)
                elif 200 <= status < 300:
                    fired += 1
                else:
                    logger.warning("room reminder fire for space %s returned %s", space_id, status)
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return fired


def _tick() -> None:
    base_url = settings.dashboard_internal_url
    if not settings.room_reminder_sweeper_enabled or not base_url:
        return
    conn = _connect()
    try:
        fired = sweep_once(conn, base_url)
        if fired:
            logger.info("room reminder sweeper fired reminders in %d room(s)", fired)
    finally:
        conn.close()


def _missing_table(error: Exception) -> bool:
    text = str(error)
    return "workspace_agent_reminders" in text or "credential_owner" in text


async def run_poller() -> None:
    """Started once from main.py's lifespan; never returns."""
    logger.info(
        "Room reminder sweeper started (enabled=%s, interval=%ds)",
        settings.room_reminder_sweeper_enabled,
        settings.room_reminder_sweep_interval_seconds,
    )
    while True:
        try:
            await asyncio.to_thread(_tick)
        except Exception as e:
            # Before the dashboard migration there is nothing to sweep: not an error.
            if _missing_table(e):
                logger.debug("room reminder sweeper: dashboard migration not applied yet")
            else:
                logger.error("Room reminder sweep failed: %s", e)
        await asyncio.sleep(max(15, settings.room_reminder_sweep_interval_seconds))

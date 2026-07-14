"""
Credit ledger — meters LLM usage for deployable agents (and future features).

Tables live in the `billing` schema of avry-postgres:
    billing.user_credits  — one row per user: balance + monthly allowance
    billing.credit_ledger — append-only transaction log

Allowances follow the pricing-page credit numbers per tier and reset lazily
at the start of each calendar month (no cron needed: the first consume/status
call in a new month resets the balance). Superadmins are unlimited.

Concurrency: `consume()` takes a row lock (SELECT ... FOR UPDATE) inside one
transaction, so two agent messages landing at the same instant can never
double-spend the same credit.

Uses sync psycopg2 per call (same pattern as telegram_service) because the
prod entrypoint doesn't run the asyncpg lifespan pool.
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Monthly credit allowance per tier — mirrors the pricing page
# (foundation $20/80cr, pro $44/220cr, enterprise $499/3000cr).
TIER_ALLOWANCES = {"foundation": 80, "pro": 220, "enterprise": 3000}
DEFAULT_ALLOWANCE = TIER_ALLOWANCES["foundation"]

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS billing.user_credits (
    user_id      TEXT PRIMARY KEY,
    balance      INTEGER NOT NULL DEFAULT 0,
    allowance    INTEGER NOT NULL DEFAULT 0,
    period_start DATE NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS billing.credit_ledger (
    id         BIGSERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL,
    delta      INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    meta       JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS credit_ledger_user_created_idx
    ON billing.credit_ledger (user_id, created_at DESC);
"""

_schema_ready = False


class InsufficientCredits(Exception):
    def __init__(self, balance: int):
        self.balance = balance
        super().__init__(f"insufficient credits (balance={balance})")


def _connect():
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set — credit ledger requires Postgres")
    return psycopg2.connect(dsn, connect_timeout=5)


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_SQL)
    conn.commit()
    _schema_ready = True


def _resolve_access(cur, user_id: str) -> dict:
    """Current tier + superadmin flag straight from identity tables."""
    cur.execute(
        """
        SELECT u.account_type, u.is_superadmin, t.tier, t.expires_at
        FROM identity.users u
        LEFT JOIN identity.user_tiers t ON t.user_id = u.id
        WHERE u.id = %s
        """,
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return {"tier": "foundation", "superadmin": False, "known": False}
    account_type, is_superadmin, tier, expires_at = row
    superadmin = bool(is_superadmin) or str(account_type or "").lower() == "superadmin"
    cur.execute("SELECT now()")
    now = cur.fetchone()[0]
    if expires_at is not None and expires_at < now:
        tier = None  # entitlement lapsed -> base tier
    return {
        "tier": str(tier or "foundation").lower(),
        "superadmin": superadmin,
        "known": True,
    }


def _locked_row(cur, user_id: str, allowance: int):
    """Fetch-or-create the user's credit row for this month, row-locked.

    Handles the lazy monthly reset and mid-period tier changes (allowance
    drift is applied to the balance so upgrades take effect immediately).
    Returns the current balance.
    """
    cur.execute(
        "SELECT balance, allowance, period_start FROM billing.user_credits WHERE user_id = %s FOR UPDATE",
        (user_id,),
    )
    row = cur.fetchone()
    cur.execute("SELECT date_trunc('month', now())::date")
    month_start = cur.fetchone()[0]

    if row is None:
        cur.execute(
            """
            INSERT INTO billing.user_credits (user_id, balance, allowance, period_start)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO NOTHING
            """,
            (user_id, allowance, allowance, month_start),
        )
        # Re-lock: another request may have won the insert race
        cur.execute(
            "SELECT balance, allowance, period_start FROM billing.user_credits WHERE user_id = %s FOR UPDATE",
            (user_id,),
        )
        row = cur.fetchone()

    balance, stored_allowance, period_start = row

    if period_start < month_start:
        # New month: fresh allowance
        balance = allowance
        cur.execute(
            "UPDATE billing.user_credits SET balance=%s, allowance=%s, period_start=%s, updated_at=now() WHERE user_id=%s",
            (balance, allowance, month_start, user_id),
        )
    elif stored_allowance != allowance:
        # Tier changed mid-period: shift the balance by the allowance delta
        balance = max(0, balance + (allowance - stored_allowance))
        cur.execute(
            "UPDATE billing.user_credits SET balance=%s, allowance=%s, updated_at=now() WHERE user_id=%s",
            (balance, allowance, user_id),
        )
    return balance


def get_status(user_id: str) -> dict:
    """Balance/allowance snapshot (creates the row if missing)."""
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            access = _resolve_access(cur, user_id)
            if access["superadmin"]:
                conn.commit()
                return {"unlimited": True, "tier": access["tier"], "balance": None, "allowance": None}
            allowance = TIER_ALLOWANCES.get(access["tier"], DEFAULT_ALLOWANCE)
            balance = _locked_row(cur, user_id, allowance)
        conn.commit()
        return {"unlimited": False, "tier": access["tier"], "balance": balance, "allowance": allowance}
    finally:
        conn.close()


def consume(user_id: str, amount: int, reason: str, meta: Optional[dict] = None) -> dict:
    """Atomically deduct `amount` credits; raises InsufficientCredits when short."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            access = _resolve_access(cur, user_id)
            if access["superadmin"]:
                conn.commit()
                return {"unlimited": True, "tier": access["tier"], "balance": None}
            allowance = TIER_ALLOWANCES.get(access["tier"], DEFAULT_ALLOWANCE)
            balance = _locked_row(cur, user_id, allowance)
            if balance < amount:
                conn.rollback()
                raise InsufficientCredits(balance)
            balance -= amount
            cur.execute(
                "UPDATE billing.user_credits SET balance=%s, updated_at=now() WHERE user_id=%s",
                (balance, user_id),
            )
            cur.execute(
                "INSERT INTO billing.credit_ledger (user_id, delta, reason, meta) VALUES (%s, %s, %s, %s)",
                (user_id, -amount, reason, json.dumps(meta or {}, ensure_ascii=False)[:4000]),
            )
        conn.commit()
        return {"unlimited": False, "tier": access["tier"], "balance": balance, "allowance": allowance}
    finally:
        conn.close()


def grant(user_id: str, amount: int, reason: str = "manual_grant", meta: Optional[dict] = None) -> dict:
    """Add credits on top of the current balance (top-ups, purchases, admin)."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            access = _resolve_access(cur, user_id)
            allowance = TIER_ALLOWANCES.get(access["tier"], DEFAULT_ALLOWANCE)
            balance = _locked_row(cur, user_id, allowance)
            balance += amount
            cur.execute(
                "UPDATE billing.user_credits SET balance=%s, updated_at=now() WHERE user_id=%s",
                (balance, user_id),
            )
            cur.execute(
                "INSERT INTO billing.credit_ledger (user_id, delta, reason, meta) VALUES (%s, %s, %s, %s)",
                (user_id, amount, reason, json.dumps(meta or {}, ensure_ascii=False)[:4000]),
            )
        conn.commit()
        return {"balance": balance, "allowance": allowance}
    finally:
        conn.close()

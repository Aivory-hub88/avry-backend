"""
Credit ledger — meters LLM usage for deployable agents (and future features).

Tables live in the `billing` schema of avry-postgres:
    billing.user_credits  — one row per user: two balances + monthly allowance
    billing.credit_ledger — append-only transaction log

A user holds two balances:
    balance        — the plan's monthly allowance. Resets lazily at the start
                     of each calendar month (the first consume/status call in
                     a new month refills it; no cron needed).
    extra_balance  — credits bought as top-up packs. Never reset or expired.
Spending draws the monthly balance first, then the extra balance.

A credit is a unit of LLM spend (`tiers.CREDIT_COST_USD`), not a message.
Rows still in the old one-credit-per-message units are converted the first
time they are touched (`units_version`, see `_convert_legacy`).

Superadmins are unlimited.

Concurrency: every write takes a row lock (SELECT ... FOR UPDATE) inside one
transaction, so two agent messages landing at the same instant can never
double-spend the same credit.

Uses sync psycopg2 per call (same pattern as telegram_service) because the
prod entrypoint doesn't run the asyncpg lifespan pool.
"""

import json
import logging
import os
from datetime import date
from typing import Optional, Tuple

from app.services import tiers

logger = logging.getLogger(__name__)

# Monthly credit allowance per tier — mirrors the pricing page
# (operational $20/1,500cr, business $99/6,500cr, enterprise sales-assisted).
# The table itself lives in app/services/tiers.py so the gates, the allowances
# and the published prices cannot drift apart again; the previous comment here
# still quoted $20/$44, prices that had not been charged for some time.
# Allowance lookups below deliberately use `tiers.normalise`, NOT
# `tiers.account_tier`: normalise keeps the base-tier fallback, so an account
# with no live plan still seeds the same monthly Intelligence Credit allowance
# it always has. `account_tier` is for gating feature ACCESS; swapping it in
# here would change how many credits an account is seeded with, which is a
# separate product decision and is not part of closing the access leak.
TIER_ALLOWANCES = tiers.TIER_ALLOWANCES
DEFAULT_ALLOWANCE = TIER_ALLOWANCES[tiers.BASE_TIER]

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
ALTER TABLE billing.user_credits
    ADD COLUMN IF NOT EXISTS extra_balance INTEGER NOT NULL DEFAULT 0;
-- 1 = one credit per agent message (before 2026-11), 2 = one credit per
-- tiers.CREDIT_COST_USD of LLM spend. Existing rows default to 1.
ALTER TABLE billing.user_credits
    ADD COLUMN IF NOT EXISTS units_version SMALLINT NOT NULL DEFAULT 1;
"""

UNITS_VERSION = 2

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
        return {"tier": tiers.FREE_TIER, "superadmin": False, "known": False}
    account_type, is_superadmin, tier, expires_at = row
    superadmin = bool(is_superadmin) or str(account_type or "").lower() == "superadmin"
    cur.execute("SELECT now()")
    now = cur.fetchone()[0]
    if expires_at is not None and expires_at < now:
        tier = None  # entitlement lapsed -> no plan
    return {
        "tier": tiers.account_tier(tier),
        "superadmin": superadmin,
        "known": True,
    }


def _convert_legacy(balance: int, stored_allowance: int, allowance: int) -> Tuple[int, int]:
    """(monthly, extra) in current units from a one-credit-per-message row.

    The old row kept purchased credits in the same balance as the allowance,
    so anything above the stored allowance was bought. The unspent share of
    the old allowance carries over as the same share of the new one.
    """
    extra_old = max(0, balance - stored_allowance)
    monthly_old = max(0, balance - extra_old)
    if stored_allowance > 0:
        monthly = round(allowance * monthly_old / stored_allowance)
    else:
        monthly = allowance
    return monthly, extra_old * tiers.LEGACY_CREDIT_FACTOR


def _locked_row(cur, user_id: str, allowance: int) -> Tuple[int, int, date]:
    """Fetch-or-create the user's credit row for this month, row-locked.

    Handles the legacy unit conversion, the lazy monthly reset and mid-period
    tier changes (allowance drift is applied to the monthly balance so
    upgrades take effect immediately). Only the monthly balance is ever reset;
    purchased credits survive month boundaries.
    Returns (monthly_balance, extra_balance, period_start).
    """
    select = (
        "SELECT balance, extra_balance, allowance, period_start, units_version "
        "FROM billing.user_credits WHERE user_id = %s FOR UPDATE"
    )
    cur.execute(select, (user_id,))
    row = cur.fetchone()
    cur.execute("SELECT date_trunc('month', now())::date")
    month_start = cur.fetchone()[0]

    if row is None:
        cur.execute(
            """
            INSERT INTO billing.user_credits
                (user_id, balance, extra_balance, allowance, period_start, units_version)
            VALUES (%s, %s, 0, %s, %s, %s)
            ON CONFLICT (user_id) DO NOTHING
            """,
            (user_id, allowance, allowance, month_start, UNITS_VERSION),
        )
        # Re-lock: another request may have won the insert race
        cur.execute(select, (user_id,))
        row = cur.fetchone()

    balance, extra, stored_allowance, period_start, units_version = row

    if units_version < UNITS_VERSION:
        balance, extra = _convert_legacy(balance, stored_allowance, allowance)
        stored_allowance = allowance
        cur.execute(
            "UPDATE billing.user_credits SET balance=%s, extra_balance=%s, allowance=%s, "
            "units_version=%s, updated_at=now() WHERE user_id=%s",
            (balance, extra, allowance, UNITS_VERSION, user_id),
        )

    if period_start < month_start:
        # New month: fresh allowance; bought credits are untouched
        balance = allowance
        period_start = month_start
        cur.execute(
            "UPDATE billing.user_credits SET balance=%s, allowance=%s, period_start=%s, updated_at=now() WHERE user_id=%s",
            (balance, allowance, month_start, user_id),
        )
    elif stored_allowance != allowance:
        # Tier changed mid-period: shift the monthly balance by the allowance delta
        balance = max(0, balance + (allowance - stored_allowance))
        cur.execute(
            "UPDATE billing.user_credits SET balance=%s, allowance=%s, updated_at=now() WHERE user_id=%s",
            (balance, allowance, user_id),
        )
    return balance, extra, period_start


def _next_month(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _month_usage(cur, user_id: str, period_start: date) -> Tuple[int, int, int]:
    """(credits spent, of which from the extra balance, LLM tokens) since `period_start`."""
    cur.execute(
        """
        SELECT COALESCE(SUM(-delta), 0),
               COALESCE(SUM(CASE WHEN meta->>'extra' ~ '^[0-9]+$'
                                 THEN (meta->>'extra')::bigint ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN meta->>'tokens' ~ '^[0-9]+$'
                                 THEN (meta->>'tokens')::bigint ELSE 0 END), 0)
        FROM billing.credit_ledger
        WHERE user_id = %s AND delta < 0 AND created_at >= %s
        """,
        (user_id, period_start),
    )
    spent, extra_spent, tokens = cur.fetchone()
    return int(spent), int(extra_spent), int(tokens)


def _allowance_for(access: dict) -> int:
    return TIER_ALLOWANCES.get(tiers.normalise(access["tier"]), DEFAULT_ALLOWANCE)


def get_status(user_id: str) -> dict:
    """Balance/allowance snapshot (creates the row if missing).

    `balance` is the total spendable (monthly + extra); callers that only gate
    on "anything left?" keep reading it unchanged.
    """
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            access = _resolve_access(cur, user_id)
            if access["superadmin"]:
                conn.commit()
                return {"unlimited": True, "tier": access["tier"], "balance": None, "allowance": None}
            allowance = _allowance_for(access)
            monthly, extra, period_start = _locked_row(cur, user_id, allowance)
            spent, extra_spent, tokens = _month_usage(cur, user_id, period_start)
        conn.commit()
        return {
            "unlimited": False,
            "tier": access["tier"],
            "balance": monthly + extra,
            "allowance": allowance,
            "monthly_balance": monthly,
            "extra_balance": extra,
            "period_start": period_start.isoformat(),
            "resets_at": _next_month(period_start).isoformat(),
            "credits_used_this_period": spent,
            "extra_used_this_period": extra_spent,
            "tokens_used_this_period": tokens,
            "tokens_per_credit": {
                "peak": tiers.TOKENS_PER_CREDIT_PEAK,
                "offpeak": tiers.TOKENS_PER_CREDIT_OFFPEAK,
            },
        }
    finally:
        conn.close()


def consume(
    user_id: str,
    amount: int,
    reason: str,
    meta: Optional[dict] = None,
    allow_partial: bool = False,
) -> dict:
    """Atomically deduct `amount` credits, monthly balance first.

    Raises InsufficientCredits when short, unless `allow_partial`: then it
    takes whatever is left. That is for settling a turn the LLM has already
    run — the spend happened, so it is charged as far as the balance goes
    rather than refused.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            access = _resolve_access(cur, user_id)
            if access["superadmin"]:
                conn.commit()
                return {"unlimited": True, "tier": access["tier"], "balance": None, "charged": 0}
            allowance = _allowance_for(access)
            monthly, extra, _ = _locked_row(cur, user_id, allowance)
            total = monthly + extra
            if total < amount and not allow_partial:
                conn.rollback()
                raise InsufficientCredits(total)
            take = min(amount, total)
            from_monthly = min(monthly, take)
            from_extra = take - from_monthly
            monthly -= from_monthly
            extra -= from_extra
            if take:
                cur.execute(
                    "UPDATE billing.user_credits SET balance=%s, extra_balance=%s, updated_at=now() WHERE user_id=%s",
                    (monthly, extra, user_id),
                )
                ledger_meta = dict(meta or {})
                ledger_meta.update(monthly=from_monthly, extra=from_extra)
                if take < amount:
                    ledger_meta["requested"] = amount
                cur.execute(
                    "INSERT INTO billing.credit_ledger (user_id, delta, reason, meta) VALUES (%s, %s, %s, %s)",
                    (user_id, -take, reason, json.dumps(ledger_meta, ensure_ascii=False)[:4000]),
                )
        conn.commit()
        return {
            "unlimited": False,
            "tier": access["tier"],
            "balance": monthly + extra,
            "allowance": allowance,
            "monthly_balance": monthly,
            "extra_balance": extra,
            "charged": take,
        }
    finally:
        conn.close()


def grant(user_id: str, amount: int, reason: str = "manual_grant", meta: Optional[dict] = None) -> dict:
    """Add credits to the extra balance (top-ups, purchases, admin).

    Granted credits never reset with the month.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            access = _resolve_access(cur, user_id)
            allowance = _allowance_for(access)
            monthly, extra, _ = _locked_row(cur, user_id, allowance)
            extra += amount
            cur.execute(
                "UPDATE billing.user_credits SET extra_balance=%s, updated_at=now() WHERE user_id=%s",
                (extra, user_id),
            )
            cur.execute(
                "INSERT INTO billing.credit_ledger (user_id, delta, reason, meta) VALUES (%s, %s, %s, %s)",
                (user_id, amount, reason, json.dumps(meta or {}, ensure_ascii=False)[:4000]),
            )
        conn.commit()
        return {
            "balance": monthly + extra,
            "allowance": allowance,
            "monthly_balance": monthly,
            "extra_balance": extra,
        }
    finally:
        conn.close()

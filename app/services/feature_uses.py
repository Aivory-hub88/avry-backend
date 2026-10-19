"""
How many times a customer may run what they bought.

Each one-time product includes USES_PER_PURCHASE runs:

    ai_snapshot   -> 3 Business Operations Assessments   (feature "snapshot")
    ai_blueprint  -> 3 Transformation Blueprints + Roadmaps (feature "blueprint")
    ai_fullstack  -> 3 full cycles: 3 assessments and 3 blueprints

Before this, buying a product turned a flag on for good and the generators had
no gate at all, so "per use" on the pricing page was not true.

The ledger is append-only: +USES_PER_PURCHASE per purchase, -1 per delivered
run. A run is charged when its result is delivered, not when it is queued, so a
failed job never costs a use and there is no refund path to abuse. `ref` (the
job id) makes a charge idempotent: polling the same finished job again does not
charge twice.

Unlimited: superadmins/admins, and partner accounts while their partner access
(a live user_tiers plan) lasts. Customers who bought before this ledger existed
are seeded with USES_PER_PURCHASE the first time they are looked at (partners
excepted: their features came free with partner access).

Sync psycopg2 per call, like credit_service and entitlement_state.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)

USES_PER_PURCHASE = 3
FEATURES = ("snapshot", "blueprint")

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS billing;
CREATE TABLE IF NOT EXISTS billing.feature_uses (
    id         BIGSERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL,
    feature    TEXT NOT NULL CHECK (feature IN ('snapshot', 'blueprint')),
    delta      INTEGER NOT NULL,
    reason     TEXT NOT NULL,           -- purchase | legacy | use
    ref        TEXT,                    -- order id, user id (legacy) or job id
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS feature_uses_ref_idx
    ON billing.feature_uses (feature, reason, ref) WHERE ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS feature_uses_user_idx
    ON billing.feature_uses (user_id, feature);
"""

_schema_ready = False


class NoUsesLeft(Exception):
    """The customer has used every run of this feature they bought."""


def _connect():
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set — feature uses require Postgres")
    return psycopg2.connect(dsn, connect_timeout=5)


def ensure_schema(cur) -> None:
    global _schema_ready
    if _schema_ready:
        return
    cur.execute(SCHEMA_SQL)
    _schema_ready = True


def grant_purchase(cur, user_id: str, features, order_id: str) -> None:
    """Add USES_PER_PURCHASE runs per feature for one order, inside the caller's
    transaction. Idempotent on (feature, order_id)."""
    ensure_schema(cur)
    for feature in features:
        if feature not in FEATURES:
            continue
        cur.execute(
            """
            INSERT INTO billing.feature_uses (user_id, feature, delta, reason, ref)
            VALUES (%s, %s, %s, 'purchase', %s)
            ON CONFLICT DO NOTHING
            """,
            (user_id, feature, USES_PER_PURCHASE, order_id),
        )


def _unlimited(cur, user_id: str) -> bool:
    cur.execute(
        """
        SELECT u.account_type, u.is_superadmin,
               t.tier IS NOT NULL AND (t.expires_at IS NULL OR t.expires_at > now())
        FROM identity.users u
        LEFT JOIN identity.user_tiers t ON t.user_id = u.id
        WHERE u.id = %s
        """,
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return False
    account_type, is_superadmin, live_plan = row
    account_type = (account_type or "").lower()
    if is_superadmin or account_type in ("superadmin", "admin"):
        return True
    return account_type == "partner" and bool(live_plan)


def _seed_legacy(cur, user_id: str, feature: str) -> None:
    """Customers who bought before the ledger: USES_PER_PURCHASE from now on."""
    cur.execute(
        "SELECT 1 FROM billing.feature_uses WHERE user_id = %s AND feature = %s LIMIT 1",
        (user_id, feature),
    )
    if cur.fetchone():
        return
    # A partner's features came free with its partner access, not from a
    # purchase, so they never seed runs: once that access lapses a partner
    # buys like anyone else.
    cur.execute(
        """
        SELECT COALESCE(t.features, '[]'::jsonb) ? %s
        FROM identity.user_tiers t
        JOIN identity.users u ON u.id = t.user_id
        WHERE t.user_id = %s AND COALESCE(u.account_type, '') <> 'partner'
        """,
        (feature, user_id),
    )
    row = cur.fetchone()
    if row and row[0]:
        cur.execute(
            """
            INSERT INTO billing.feature_uses (user_id, feature, delta, reason, ref)
            VALUES (%s, %s, %s, 'legacy', %s)
            ON CONFLICT DO NOTHING
            """,
            (user_id, feature, USES_PER_PURCHASE, f"legacy:{user_id}"),
        )


def _balance(cur, user_id: str, feature: str) -> Dict[str, int]:
    cur.execute(
        """
        SELECT COALESCE(sum(delta), 0),
               COALESCE(sum(delta) FILTER (WHERE delta > 0), 0),
               count(*) FILTER (WHERE reason = 'use')
        FROM billing.feature_uses WHERE user_id = %s AND feature = %s
        """,
        (user_id, feature),
    )
    remaining, granted, used = cur.fetchone()
    return {"remaining": max(int(remaining), 0), "granted": int(granted), "used": int(used)}


def _lock(cur, user_id: str, feature: str) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"feature_uses:{user_id}:{feature}",))


def status(user_id: str) -> Dict[str, Dict[str, object]]:
    """{feature: {remaining, granted, used, unlimited}} for every feature."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            ensure_schema(cur)
            unlimited = _unlimited(cur, user_id)
            out: Dict[str, Dict[str, object]] = {}
            for feature in FEATURES:
                _lock(cur, user_id, feature)
                _seed_legacy(cur, user_id, feature)
                out[feature] = {**_balance(cur, user_id, feature), "unlimited": unlimited}
        conn.commit()
        return out
    finally:
        conn.close()


def consume(user_id: str, feature: str, ref: str) -> Dict[str, object]:
    """
    Charge one run of `feature` for job `ref`. Idempotent on `ref`.
    Raises NoUsesLeft when nothing is left (and nothing is charged).
    """
    if feature not in FEATURES:
        raise ValueError(f"unknown feature {feature!r}")
    if not ref:
        raise ValueError("ref is required")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            ensure_schema(cur)
            if _unlimited(cur, user_id):
                conn.commit()
                return {"charged": False, "unlimited": True, "remaining": None}
            _lock(cur, user_id, feature)
            cur.execute(
                "SELECT user_id FROM billing.feature_uses "
                "WHERE feature = %s AND reason = 'use' AND ref = %s",
                (feature, ref),
            )
            existing = cur.fetchone()
            if existing:
                if existing[0] != user_id:
                    raise NoUsesLeft()  # someone else's job id: never deliver it
                bal = _balance(cur, user_id, feature)
                conn.commit()
                return {"charged": False, "unlimited": False, **bal}
            _seed_legacy(cur, user_id, feature)
            bal = _balance(cur, user_id, feature)
            if bal["remaining"] <= 0:
                conn.rollback()
                raise NoUsesLeft()
            cur.execute(
                """
                INSERT INTO billing.feature_uses (user_id, feature, delta, reason, ref)
                VALUES (%s, %s, -1, 'use', %s)
                """,
                (user_id, feature, ref),
            )
            bal = _balance(cur, user_id, feature)
        conn.commit()
        return {"charged": True, "unlimited": False, **bal}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

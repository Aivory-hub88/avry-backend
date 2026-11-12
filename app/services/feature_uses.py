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

Free accounts (since 2026-11-12, accounts with no purchase may sign in and
see the dashboard as a preview) get a one-time FREE_GRANTS allowance the first
time they are looked at: 2 diagnostics and 1 workflow-copilot session. It is
granted once per account (unique ref), never to staff or partners, and never
to an account that already bought something.

`copilot` is only ever charged for accounts without a paid plan; the dashboard
lets paying accounts use the copilot freely. A copilot session has no single
"result" to charge on, so it uses the trial calls instead (trial_check before
each turn, trial_record after a turn that worked): the session's one use is
charged on its first successful turn, and billing.trial_turns caps the turns
inside that session at TRIAL_MAX_TURNS. A failed turn costs nothing.

Sync psycopg2 per call, like credit_service and entitlement_state.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)

USES_PER_PURCHASE = 3
FEATURES = ("snapshot", "blueprint", "copilot")
#: One-time allowance for an account that has never bought anything.
FREE_GRANTS = {"snapshot": 2, "copilot": 1}
#: Turns allowed inside one trial session (enough to clarify, generate, test
#: and fix once). Only features listed here can use the trial calls.
TRIAL_MAX_TURNS = {"copilot": 12}

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS billing;
CREATE TABLE IF NOT EXISTS billing.feature_uses (
    id         BIGSERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL,
    feature    TEXT NOT NULL CHECK (feature IN ('snapshot', 'blueprint', 'copilot')),
    delta      INTEGER NOT NULL,
    reason     TEXT NOT NULL,           -- purchase | legacy | free | use
    ref        TEXT,                    -- order id, user id (legacy) or job id
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS feature_uses_ref_idx
    ON billing.feature_uses (feature, reason, ref) WHERE ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS feature_uses_user_idx
    ON billing.feature_uses (user_id, feature);
CREATE TABLE IF NOT EXISTS billing.trial_turns (
    user_id    TEXT NOT NULL,
    feature    TEXT NOT NULL,
    ref        TEXT NOT NULL,           -- the session id the use was charged for
    turns      INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, feature, ref)
);
"""

# Tables created before `copilot` existed carry the two-feature CHECK.
_WIDEN_FEATURE_CHECK_SQL = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'billing.feature_uses'::regclass
          AND conname = 'feature_uses_feature_check'
          AND pg_get_constraintdef(oid) NOT LIKE '%copilot%'
    ) THEN
        ALTER TABLE billing.feature_uses DROP CONSTRAINT feature_uses_feature_check;
        ALTER TABLE billing.feature_uses ADD CONSTRAINT feature_uses_feature_check
            CHECK (feature IN ('snapshot', 'blueprint', 'copilot'));
    END IF;
END $$;
"""

_schema_ready = False


class NoUsesLeft(Exception):
    """The customer has used every run of this feature they bought."""


class TrialTurnsUsed(NoUsesLeft):
    """Every turn of the trial session has been used."""


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
    cur.execute(_WIDEN_FEATURE_CHECK_SQL)
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


def _seed_free(cur, user_id: str, feature: str) -> None:
    """The one-time free allowance, for an account that never bought anything."""
    amount = FREE_GRANTS.get(feature)
    if not amount:
        return
    ref = f"free:{feature}:{user_id}"
    cur.execute(
        "SELECT 1 FROM billing.feature_uses WHERE feature = %s AND reason = 'free' AND ref = %s",
        (feature, ref),
    )
    if cur.fetchone():
        return
    cur.execute(
        """
        SELECT COALESCE(u.account_type, ''),
               COALESCE(u.is_superadmin, false),
               EXISTS (SELECT 1 FROM billing.feature_uses f
                        WHERE f.user_id = u.id AND f.reason IN ('purchase', 'legacy')),
               EXISTS (SELECT 1 FROM identity.user_tiers t
                        WHERE t.user_id = u.id
                          AND (t.expires_at IS NULL OR t.expires_at > now())
                          AND COALESCE(t.tier, 'free') <> 'free')
        FROM identity.users u WHERE u.id = %s
        """,
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return
    account_type, is_superadmin, bought, live_plan = row
    if is_superadmin or str(account_type).lower() in ("admin", "superadmin", "employee", "demo", "partner"):
        return
    if bought or live_plan:
        return
    cur.execute(
        """
        INSERT INTO billing.feature_uses (user_id, feature, delta, reason, ref)
        VALUES (%s, %s, %s, 'free', %s)
        ON CONFLICT DO NOTHING
        """,
        (user_id, feature, amount, ref),
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
                _seed_free(cur, user_id, feature)
                out[feature] = {**_balance(cur, user_id, feature), "unlimited": unlimited}
        conn.commit()
        return out
    finally:
        conn.close()


def _charge(cur, user_id: str, feature: str, ref: str) -> Dict[str, object]:
    """One idempotent charge inside the caller's transaction (lock held)."""
    cur.execute(
        "SELECT user_id FROM billing.feature_uses "
        "WHERE feature = %s AND reason = 'use' AND ref = %s",
        (feature, ref),
    )
    existing = cur.fetchone()
    if existing:
        if existing[0] != user_id:
            raise NoUsesLeft()  # someone else's job id: never deliver it
        return {"charged": False, "unlimited": False, **_balance(cur, user_id, feature)}
    _seed_legacy(cur, user_id, feature)
    _seed_free(cur, user_id, feature)
    if _balance(cur, user_id, feature)["remaining"] <= 0:
        raise NoUsesLeft()
    cur.execute(
        """
        INSERT INTO billing.feature_uses (user_id, feature, delta, reason, ref)
        VALUES (%s, %s, -1, 'use', %s)
        """,
        (user_id, feature, ref),
    )
    return {"charged": True, "unlimited": False, **_balance(cur, user_id, feature)}


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
            result = _charge(cur, user_id, feature, ref)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _trial_args(feature: str, ref: str) -> int:
    if feature not in TRIAL_MAX_TURNS:
        raise ValueError(f"no trial for feature {feature!r}")
    if not ref:
        raise ValueError("ref is required")
    return TRIAL_MAX_TURNS[feature]


def _turns(cur, user_id: str, feature: str, ref: str) -> int:
    cur.execute(
        "SELECT turns FROM billing.trial_turns WHERE user_id = %s AND feature = %s AND ref = %s",
        (user_id, feature, ref),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def trial_check(user_id: str, feature: str, ref: str) -> Dict[str, object]:
    """
    May the caller run one more turn of trial session `ref`? Charges nothing.
    Allowed when the session is already paid for and has turns left, or when
    it is new and a use is left to pay for it. Raises NoUsesLeft (or
    TrialTurnsUsed) otherwise.
    """
    max_turns = _trial_args(feature, ref)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            ensure_schema(cur)
            if _unlimited(cur, user_id):
                conn.commit()
                return {"allowed": True, "unlimited": True, "turns_left": None}
            _lock(cur, user_id, feature)
            cur.execute(
                "SELECT user_id FROM billing.feature_uses "
                "WHERE feature = %s AND reason = 'use' AND ref = %s",
                (feature, ref),
            )
            owner = cur.fetchone()
            if owner and owner[0] != user_id:
                raise NoUsesLeft()
            if not owner:
                _seed_legacy(cur, user_id, feature)
                _seed_free(cur, user_id, feature)
                if _balance(cur, user_id, feature)["remaining"] <= 0:
                    raise NoUsesLeft()
            turns = _turns(cur, user_id, feature, ref)
            if turns >= max_turns:
                raise TrialTurnsUsed()
        conn.commit()
        return {"allowed": True, "unlimited": False, "turns_left": max_turns - turns}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def trial_record(user_id: str, feature: str, ref: str) -> Dict[str, object]:
    """
    Record one successful turn of trial session `ref`: charges the session's
    use on its first turn (idempotent on `ref`) and counts the turn, in one
    transaction. Raises NoUsesLeft / TrialTurnsUsed when the turn must not be
    delivered (e.g. two sessions raced for the last use).
    """
    max_turns = _trial_args(feature, ref)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            ensure_schema(cur)
            if _unlimited(cur, user_id):
                conn.commit()
                return {"charged": False, "unlimited": True, "turns_left": None}
            _lock(cur, user_id, feature)
            charge = _charge(cur, user_id, feature, ref)
            turns = _turns(cur, user_id, feature, ref)
            if turns >= max_turns:
                raise TrialTurnsUsed()
            cur.execute(
                """
                INSERT INTO billing.trial_turns (user_id, feature, ref, turns)
                VALUES (%s, %s, %s, 1)
                ON CONFLICT (user_id, feature, ref)
                DO UPDATE SET turns = billing.trial_turns.turns + 1, updated_at = now()
                """,
                (user_id, feature, ref),
            )
        conn.commit()
        return {"charged": charge["charged"], "unlimited": False, "turns_left": max_turns - turns - 1}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

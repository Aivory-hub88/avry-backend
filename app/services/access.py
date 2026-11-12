"""
What an account may use right now: one rule, one database read.

Before this module the answer was spread over three places that disagreed at
the edges: `/auth/me` scanned the legacy JSON stores (payments, diagnostics,
snapshots, blueprints, all empty on prod since payments moved to Postgres) on
every call and then overlaid `identity.user_tiers` with two fresh connections;
the credit ledger kept its own reader; and when the entitlement table could not
be read, `/auth/me` quietly answered `free` for a paying customer. A gate that
trusts that answer shows "upgrade" to people who already paid.

`decide()` is the pure rule. `read()` is the strict loader for gates: it RAISES
`AccessUnavailable` instead of guessing, so a gate can answer 503 rather than
402. `/auth/me` uses `decide()` on its own best-effort read for display.

Rule (unchanged from what `/auth/me` already returned on prod):
  - superadmin                  -> enterprise
  - live subscription row       -> its canonical tier (aliases resolved)
  - partner, no live plan       -> free (free-period features don't count)
  - bought blueprint / snapshot -> that one-time tier (they never expire)
  - otherwise                   -> free
`paid` is what the dashboard's paid-plan gate needs: any tier but `free`, or a
staff / demo account.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Iterable, Optional

from app.services import tiers

logger = logging.getLogger(__name__)

STAFF_ACCOUNT_TYPES = frozenset({"superadmin", "admin", "employee"})
#: One-time products, best first. Buying the blueprint put a user on the
#: `blueprint` tier in the legacy flow, so it outranks the snapshot.
ONE_TIME_TIERS = ("blueprint", "snapshot")


class AccessUnavailable(Exception):
    """The entitlement tables could not be read. Not the same as "no plan"."""


def decide(
    account_type: Optional[str],
    *,
    is_superadmin: bool = False,
    row_tier: Optional[str] = None,
    features: Iterable[str] = (),
    expired: bool = False,
) -> Dict[str, Any]:
    """Pure: fold an account and its `user_tiers` row into one access answer."""
    kind = str(account_type or "").strip().lower()
    superadmin = bool(is_superadmin) or kind == "superadmin"
    staff = superadmin or kind in STAFF_ACCOUNT_TYPES
    feats = sorted({str(f).strip().lower() for f in (features or []) if f})

    live = None if expired else tiers.account_tier(row_tier)
    subscription = None if live in (None, tiers.FREE_TIER) else live

    if superadmin:
        tier, subscription = "enterprise", "enterprise"
    elif subscription:
        tier = subscription
    elif kind == "partner":
        tier = tiers.FREE_TIER
    else:
        tier = next((t for t in ONE_TIME_TIERS if t in feats), tiers.FREE_TIER)

    return {
        "tier": tier,
        "subscription_tier": subscription,
        "is_subscribed": subscription is not None,
        "features": feats,
        "paid": staff or kind == "demo" or tier != tiers.FREE_TIER,
        "staff": staff,
        "account_type": kind or None,
    }


def _connect():
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise AccessUnavailable("DATABASE_URL not set")
    return psycopg2.connect(dsn, connect_timeout=5)


_QUERY = """
    SELECT u.account_type, u.is_superadmin, t.tier, t.features, t.expires_at,
           (t.expires_at IS NOT NULL AND t.expires_at < now()) AS expired
    FROM identity.users u
    LEFT JOIN identity.user_tiers t ON t.user_id = u.id
    WHERE u.id = %s
    ORDER BY (t.expires_at IS NULL OR t.expires_at > now()) DESC NULLS LAST,
             t.expires_at DESC NULLS FIRST
    LIMIT 1
"""


def read(user_id: str) -> Optional[Dict[str, Any]]:
    """
    Strict loader for gates. One connection, one query.

    Returns None when the account does not exist (deleted, or a token for a
    user that was never stored). Raises `AccessUnavailable` on any database
    problem: the caller must fail closed, never treat it as `free`.
    """
    if not user_id:
        return None
    try:
        conn = _connect()
    except AccessUnavailable:
        raise
    except Exception as e:  # psycopg2.OperationalError and friends
        raise AccessUnavailable(str(e)) from e
    try:
        with conn.cursor() as cur:
            cur.execute(_QUERY, (user_id,))
            row = cur.fetchone()
    except Exception as e:
        raise AccessUnavailable(str(e)) from e
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not row:
        return None
    account_type, is_superadmin, tier, features, expires_at, expired = row
    state = decide(
        account_type,
        is_superadmin=bool(is_superadmin),
        row_tier=tier,
        features=features or [],
        expired=bool(expired),
    )
    state["expires_at"] = expires_at.isoformat() if expires_at and not expired else None
    return state

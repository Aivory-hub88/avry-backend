"""
Reading back what a payment granted.

`app/routes/entitlements.py` writes purchases into Postgres
(`identity.user_tiers.tier` / `.features`, `billing.user_credits`). Nothing read
those tables when building the user object returned by login/refresh, so a paid
customer kept seeing the free-tier UI: the money moved, the entitlement landed,
and the dashboard never noticed. This module is the read side.

Every lookup is best-effort. A missing DATABASE_URL, an unreachable database or a
user with no rows yet must degrade to "no live entitlement" rather than break
authentication, so callers can safely overlay the result on their existing
computation.

Uses sync psycopg2 per call, matching credit_service — the prod entrypoint does
not run the asyncpg lifespan pool.
"""

import logging
import os
from typing import Any, Dict, List, Optional

from app.services import tiers

logger = logging.getLogger(__name__)

# Subscription tiers that mean "currently paying".
# Canonical tier ids plus the pre-rebrand aliases still present in older
# identity.user_tiers rows, so a legacy value is still recognised as a
# subscription rather than silently treated as no plan at all.
SUBSCRIPTION_TIERS = set(tiers.CANONICAL_TIERS) | set(tiers.ALIASES)

# A feature flag alone implies a tier, mirroring the legacy JSON logic where
# buying the blueprint put the user on the `blueprint` tier.
FEATURE_TIERS = {"blueprint": "blueprint", "snapshot": "snapshot"}


def _connect():
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        return None
    return psycopg2.connect(dsn, connect_timeout=5)


def get_entitlements(user_id: str) -> Optional[Dict[str, Any]]:
    """
    The live tier and one-off features for `user_id`.

    Returns None when the entitlement tables cannot be consulted — which is not
    the same as "nothing granted", so callers must keep their fallback rather
    than treating None as an empty entitlement.

    An expired `expires_at` reports `tier=None`: a lapsed subscription is not a
    current one, but the features bought outright never expire.
    """
    if not user_id:
        return None

    try:
        conn = _connect()
    except Exception as e:
        logger.warning("Entitlement lookup unavailable for %s: %s", user_id, e)
        return None

    if conn is None:
        return None

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.tier, t.features, t.expires_at,
                       (t.expires_at IS NOT NULL AND t.expires_at < now()) AS expired
                FROM identity.user_tiers t
                WHERE t.user_id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()
    except Exception as e:
        # Includes "relation does not exist" on installs that have never taken a
        # payment; treated the same as unreachable.
        logger.warning("Entitlement lookup failed for %s: %s", user_id, e)
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not row:
        return {"tier": None, "features": [], "expired": False, "found": False}

    tier, features, expires_at, expired = row
    feature_list: List[str] = [str(f) for f in (features or []) if f]

    return {
        "tier": None if expired else (str(tier).lower() if tier else None),
        "features": feature_list,
        "expired": bool(expired),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "found": True,
    }


def get_credit_snapshot(user_id: str) -> Optional[Dict[str, Any]]:
    """
    Live credit balance/allowance, or None if the ledger can't be read.

    Superadmins are unlimited in the ledger (`balance=None`); that is passed
    through untouched so the caller decides how to display it.
    """
    try:
        from app.services import credit_service

        return credit_service.get_status(user_id)
    except Exception as e:
        logger.warning("Credit snapshot failed for %s: %s", user_id, e)
        return None


def overlay(tier_info: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """
    Merge live entitlements onto a legacy-computed `tier_info` dict.

    The merge only ever grants: a live subscription replaces the computed tier, a
    bought feature turns its flag on, and live credits replace the tier defaults.
    Nothing here can take access away, so a database blip cannot lock a paying
    customer out of something they already had.
    """
    merged = dict(tier_info)

    live = get_entitlements(user_id)
    if live is None:
        return merged

    features = set(live.get("features") or [])
    live_tier = live.get("tier")

    if live_tier in SUBSCRIPTION_TIERS:
        merged["tier"] = live_tier
        merged["is_subscribed"] = True
    else:
        # Feature-only purchases sit on tier 'free' in user_tiers; don't let that
        # overwrite a better tier the legacy computation already found.
        for feature, implied in FEATURE_TIERS.items():
            if feature in features and merged.get("tier") in (None, "", "free"):
                merged["tier"] = implied
                break

    if "snapshot" in features:
        merged["has_snapshot"] = True
        # The dashboard's "Deep Diagnostic" card reads has_diagnostic.
        merged["has_diagnostic"] = True
    if "blueprint" in features:
        merged["has_blueprint"] = True

    credits = get_credit_snapshot(user_id)
    if credits:
        if credits.get("unlimited"):
            merged["credits"] = merged.get("credits_max") or merged.get("credits") or 0
        else:
            if credits.get("balance") is not None:
                merged["credits"] = int(credits["balance"])
            if credits.get("allowance") is not None:
                merged["credits_max"] = int(credits["allowance"])

    return merged

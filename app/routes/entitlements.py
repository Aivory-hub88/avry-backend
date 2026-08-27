"""
Entitlement API — turns a *verified* payment into real access.

avry-payments owns the Midtrans conversation; this service owns identity, so
after payments has confirmed settlement with Midtrans it calls in here to apply
the effect:

    POST /api/v1/entitlements/internal/grant   (X-Internal-Token)

Idempotency is enforced in Postgres, not by the caller: `billing.entitlement_grants`
has the order_id as its primary key, so a Midtrans notification retry (or a
webhook racing the browser's confirm call) applies the grant exactly once. The
insert happens in the same transaction as the effect, so a crash rolls back both.

Product -> effect, following the pricing page and credit_service.TIER_ALLOWANCES:

    operational   -> tier 'operational' (80 credits/mo)
    business      -> tier 'business'    (220 credits/mo)
    enterprise    -> tier 'enterprise'  (3000 credits/mo)
    ai_snapshot   -> feature 'snapshot'
    ai_blueprint  -> feature 'blueprint'
    ai_fullstack  -> features 'snapshot' + 'blueprint'
    credits_<n>   -> +n credits on top of the current balance
"""

import json
import logging
import os
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.services import credit_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/entitlements", tags=["entitlements"])

# Subscription products -> the tier stored in identity.user_tiers.
#
# Product id and stored tier are now the SAME canonical string (the 2026
# rebrand names), so a grant no longer translates between two vocabularies.
# The pre-rebrand product ids are kept as read-only entries because orders
# placed before the rebrand still carry them and may be re-notified by
# Midtrans at any time.
TIER_PRODUCTS = {
    "operational": "operational",
    "business": "business",
    "enterprise": "enterprise",
    # Legacy product ids -> their canonical tier.
    "foundation": "operational",
    "pro": "business",
    "acceleration": "business",
    "intelligence": "enterprise",
}

# One-off products -> the flag(s) appended to identity.user_tiers.features.
# A product may unlock more than one feature (the bundle unlocks both), so the
# value is always a tuple even when it holds a single flag.
FEATURE_PRODUCTS = {
    "ai_snapshot": ("snapshot",),
    "ai_blueprint": ("blueprint",),
    "ai_fullstack": ("snapshot", "blueprint"),
    # Legacy ids for the same two products, used by the dashboard's feature cards.
    "ai_diagnostic": ("snapshot",),
    "ai_bundle": ("snapshot", "blueprint"),
    "full_stack": ("snapshot", "blueprint"),
}

SUBSCRIPTION_DAYS = 31

_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS billing;
CREATE TABLE IF NOT EXISTS billing.entitlement_grants (
    order_id   TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    product    TEXT NOT NULL,
    amount_usd NUMERIC(12,2),
    detail     JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS entitlement_grants_user_idx
    ON billing.entitlement_grants (user_id, created_at DESC);
"""


def require_internal_token(x_internal_token: Optional[str] = Header(None)) -> None:
    """
    Shared-secret auth for trusted server-to-server callers (avry-payments).

    Prefers a dedicated INTERNAL_SERVICE_TOKEN; falls back to the existing
    bridge secret so this endpoint works on hosts that only have that set.
    """
    expected = os.getenv("INTERNAL_SERVICE_TOKEN") or os.getenv("TELEGRAM_GATEWAY_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="Internal token not configured")
    if not x_internal_token or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(status_code=403, detail="Forbidden")


class GrantRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    product: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=1, max_length=128)
    amount_usd: float = Field(default=0, ge=0)


def _connect():
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set — entitlements require Postgres")
    return psycopg2.connect(dsn, connect_timeout=5)


def _ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_SQL)
    conn.commit()


def _credits_for(product: str) -> Optional[int]:
    if not product.startswith("credits_"):
        return None
    try:
        return int(product.split("_", 1)[1])
    except (IndexError, ValueError):
        return None


@router.post("/internal/grant", dependencies=[Depends(require_internal_token)])
def grant_entitlement(body: GrantRequest):
    """
    Apply the entitlement for a settled order, exactly once.

    Returns `{"ok": true, "already_granted": true}` when the order has already
    been applied — that is a success, not an error, because Midtrans retries.
    """
    product = body.product
    credits = _credits_for(product)

    if product not in TIER_PRODUCTS and product not in FEATURE_PRODUCTS and credits is None:
        # Wallet top-ups stay inside avry-payments; anything else is unknown and
        # must not silently succeed.
        raise HTTPException(status_code=400, detail=f"No entitlement mapping for '{product}'")

    conn = _connect()
    try:
        _ensure_schema(conn)

        with conn.cursor() as cur:
            # Claim the order first. If another caller already has it, the
            # insert affects no rows and we stop here without re-applying.
            cur.execute(
                """
                INSERT INTO billing.entitlement_grants (order_id, user_id, product, amount_usd)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (order_id) DO NOTHING
                """,
                (body.order_id, body.user_id, product, body.amount_usd),
            )
            if cur.rowcount == 0:
                conn.rollback()
                logger.info("Entitlement for order %s already granted", body.order_id)
                return {"ok": True, "already_granted": True, "order_id": body.order_id}

            cur.execute("SELECT id FROM identity.users WHERE id = %s", (body.user_id,))
            if cur.fetchone() is None:
                conn.rollback()
                raise HTTPException(status_code=404, detail=f"Unknown user {body.user_id}")

            detail: dict = {"product": product}

            if product in TIER_PRODUCTS:
                tier = TIER_PRODUCTS[product]
                cur.execute(
                    """
                    INSERT INTO identity.user_tiers (user_id, tier, expires_at, updated_at)
                    VALUES (%s, %s, now() + make_interval(days => %s), now())
                    ON CONFLICT (user_id) DO UPDATE
                       SET tier = EXCLUDED.tier,
                           -- extend from whichever is later, so renewing early
                           -- never shortens a subscription
                           expires_at = GREATEST(
                               COALESCE(identity.user_tiers.expires_at, now()), now()
                           ) + make_interval(days => %s),
                           updated_at = now()
                    RETURNING tier, expires_at
                    """,
                    (body.user_id, tier, SUBSCRIPTION_DAYS, SUBSCRIPTION_DAYS),
                )
                row = cur.fetchone()
                cur.execute(
                    "UPDATE identity.users SET account_type='paid', updated_at=now() "
                    "WHERE id=%s AND account_type NOT IN ('admin','superadmin')",
                    (body.user_id,),
                )
                detail.update({"tier": row[0], "expires_at": row[1].isoformat()})

            elif product in FEATURE_PRODUCTS:
                features = list(FEATURE_PRODUCTS[product])
                cur.execute(
                    """
                    INSERT INTO identity.user_tiers (user_id, tier, features, updated_at)
                    VALUES (%s, 'free', %s::jsonb, now())
                    ON CONFLICT (user_id) DO UPDATE
                       SET features = (
                               SELECT COALESCE(jsonb_agg(DISTINCT f), '[]'::jsonb)
                               FROM jsonb_array_elements(
                                   COALESCE(identity.user_tiers.features, '[]'::jsonb)
                                   || EXCLUDED.features
                               ) AS f
                           ),
                           updated_at = now()
                    RETURNING features
                    """,
                    (body.user_id, json.dumps(features)),
                )
                row = cur.fetchone()
                cur.execute(
                    "UPDATE identity.users SET account_type='paid', updated_at=now() "
                    "WHERE id=%s AND account_type NOT IN ('admin','superadmin')",
                    (body.user_id,),
                )
                detail.update({"granted_features": features, "features": row[0]})

            cur.execute(
                "UPDATE billing.entitlement_grants SET detail=%s WHERE order_id=%s",
                (json.dumps(detail, ensure_ascii=False)[:4000], body.order_id),
            )

        # Commit the tier/feature change together with the idempotency claim.
        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.error("Entitlement grant failed for order %s: %s", body.order_id, e)
        raise HTTPException(status_code=503, detail="Entitlement service unavailable")
    finally:
        conn.close()

    # Credits use credit_service so the row-locking and monthly-reset logic
    # stays in one place. It runs after the claim commits, so a failure here
    # surfaces as a retryable error rather than a double grant.
    if credits is not None:
        try:
            result = credit_service.grant(
                body.user_id,
                credits,
                reason="purchase",
                meta={"order_id": body.order_id, "product": product},
            )
        except Exception as e:
            logger.error("Credit grant failed for order %s: %s", body.order_id, e)
            raise HTTPException(status_code=503, detail="Credit grant failed")
        return {
            "ok": True,
            "already_granted": False,
            "order_id": body.order_id,
            "detail": {"product": product, "credits_granted": credits, **result},
        }

    return {"ok": True, "already_granted": False, "order_id": body.order_id, "detail": detail}


@router.get("/internal/{user_id}", dependencies=[Depends(require_internal_token)])
def list_grants(user_id: str):
    """Every entitlement applied to `user_id`, newest first (support/audit)."""
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT order_id, product, amount_usd, detail, created_at
                FROM billing.entitlement_grants
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 200
                """,
                (user_id,),
            )
            rows = cur.fetchall()
    except Exception as e:
        logger.error("Listing grants failed for %s: %s", user_id, e)
        raise HTTPException(status_code=503, detail="Entitlement service unavailable")
    finally:
        conn.close()

    return {
        "user_id": user_id,
        "grants": [
            {
                "order_id": r[0],
                "product": r[1],
                "amount_usd": float(r[2]) if r[2] is not None else None,
                "detail": r[3],
                "created_at": r[4].isoformat(),
            }
            for r in rows
        ],
    }

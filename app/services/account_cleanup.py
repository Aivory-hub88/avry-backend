"""
Auto-cleanup for accounts that never convert, and subscribers who stop paying.

Two independent policies:

* Policy A — a signup that never completes a purchase gets a 32-hour window.
  A warning email fires once at the 24h mark; if still unpurchased at 32h the
  account is hard-deleted. `check_payment_required()` is also called
  synchronously from `auth_service.login()` to block login for the duration
  of that window and hand the caller a deadline to show the user.
* Policy B — a subscriber whose `user_tiers.expires_at` lapses gets a 40-day
  grace period. A warning fires once at lapse, then hard-delete at lapse+40d
  if still unrenewed. Login is *not* blocked during this grace period — the
  existing `entitlement_state.get_entitlements()` already reports a lapsed
  tier as `tier=None`, so access is naturally downgraded without a hard gate.

"Never purchased" is read from `billing.payment_orders.status = 'paid'`
(avry-payments' schema, same Postgres database) — not `account_type`, which
can be stale or manually set. Staff accounts (admin/superadmin/demo) are
never eligible for either policy.

Ships with `ACCOUNT_CLEANUP_ENABLED=false`: the poller still runs on its
normal interval and logs exactly what it would warn/delete, but sends no
mail and deletes nothing until that's explicitly turned on. This is
deliberate — see the plan this was built from for the blast-radius numbers
that motivated shipping it disabled by default.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import settings
from app.database import pg_service as pg
from app.services import email_service

logger = logging.getLogger(__name__)

# Policy A: hours from signup.
POLICY_A_WARN_AFTER_HOURS = 24
POLICY_A_DELETE_AFTER_HOURS = 32

# Policy B: days of grace after a subscription's expires_at passes.
POLICY_B_GRACE_DAYS = 40

_STAFF_ACCOUNT_TYPES = ("admin", "superadmin", "demo")

_NO_PAID_ORDER_SQL = """
    NOT EXISTS (
        SELECT 1 FROM billing.payment_orders p
        WHERE p.user_id = users.id AND p.status = 'paid'
    )
"""


# ---------------------------------------------------------------------------
# Policy A — never-purchased signups
# ---------------------------------------------------------------------------

async def check_payment_required(user_id: str) -> Optional[datetime]:
    """
    Called from auth_service.login() right after password verification.

    Returns the purchase deadline (aware datetime) if this account is still
    inside its 32h no-purchase window and login should be blocked; None if
    login should proceed normally (staff, already purchased, inactive, or
    the account is simply outside Policy A's scope).
    """
    pool = await pg.get_pool()
    row = await pool.fetchrow(
        f"""
        SELECT created_at FROM users
        WHERE id = $1
          AND is_active = true
          AND account_type NOT IN ('admin', 'superadmin', 'demo')
          AND {_NO_PAID_ORDER_SQL}
        """,
        user_id,
    )
    if not row:
        return None
    return row["created_at"] + timedelta(hours=POLICY_A_DELETE_AFTER_HOURS)


async def _policy_a_warn_candidates(pool) -> list:
    return await pool.fetch(
        f"""
        SELECT id, email, created_at FROM users
        WHERE is_active = true
          AND account_type NOT IN ('admin', 'superadmin', 'demo')
          AND cleanup_warned_at IS NULL
          AND created_at <= now() - make_interval(hours => {POLICY_A_WARN_AFTER_HOURS})
          AND created_at > now() - make_interval(hours => {POLICY_A_DELETE_AFTER_HOURS})
          AND {_NO_PAID_ORDER_SQL}
        """
    )


async def _policy_a_delete_candidates(pool) -> list:
    return await pool.fetch(
        f"""
        SELECT id, email, account_type, created_at FROM users
        WHERE is_active = true
          AND account_type NOT IN ('admin', 'superadmin', 'demo')
          AND created_at <= now() - make_interval(hours => {POLICY_A_DELETE_AFTER_HOURS})
          AND {_NO_PAID_ORDER_SQL}
        """
    )


def _policy_a_email_html(deadline_local: str) -> str:
    return (
        '<!doctype html><html><body style="margin:0;padding:24px;background:#0f1310;'
        'font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#e8ece4">'
        '<div style="max-width:520px;margin:0 auto;background:#161b16;'
        'border:1px solid #2a322a;border-radius:12px;padding:32px">'
        '<h1 style="margin:0 0 16px;font-size:20px;font-weight:600;color:#ffffff">'
        'Finish setting up your Aivory account</h1>'
        '<p style="margin:0 0 12px;font-size:14px;line-height:1.6;color:#c3cbbc">'
        "You created an Aivory account but haven't completed a purchase yet. "
        "To keep your account, finish checkout by " + deadline_local + " &mdash; "
        "after that the account and its data are removed.</p>"
        '<p style="margin:0 0 24px">'
        '<a href="https://aivory.uk/pricing" style="display:inline-block;'
        'background:#b2cca2;color:#0f1310;text-decoration:none;font-weight:600;'
        'font-size:14px;padding:12px 24px;border-radius:8px">Complete your purchase</a></p>'
        '<p style="margin:0;font-size:12px;line-height:1.6;color:#7d8878">'
        "If you didn't mean to sign up, no action is needed &mdash; the account "
        "will be removed automatically.</p>"
        '</div></body></html>'
    )


async def _send_policy_a_warning(email: str, deadline: datetime) -> bool:
    deadline_local = deadline.strftime("%Y-%m-%d %H:%M UTC")
    text = (
        "You created an Aivory account but haven't completed a purchase yet.\n\n"
        f"Finish checkout by {deadline_local} to keep your account — after that "
        "it and its data are removed.\n\nhttps://aivory.uk/pricing"
    )
    return await email_service.send_email(
        email,
        "Finish setting up your Aivory account",
        _policy_a_email_html(deadline_local),
        text,
    )


# ---------------------------------------------------------------------------
# Policy B — lapsed subscriptions
# ---------------------------------------------------------------------------

async def _policy_b_warn_candidates(pool) -> list:
    return await pool.fetch(
        """
        SELECT t.user_id, u.email, t.tier, t.expires_at
        FROM user_tiers t
        JOIN users u ON u.id = t.user_id
        WHERE t.expires_at IS NOT NULL
          AND t.expires_at <= now()
          AND t.lapse_warned_at IS NULL
          AND u.is_active = true
          AND u.account_type NOT IN ('admin', 'superadmin', 'demo')
        """
    )


async def _policy_b_delete_candidates(pool) -> list:
    return await pool.fetch(
        f"""
        SELECT t.user_id, u.email, u.account_type, u.created_at
        FROM user_tiers t
        JOIN users u ON u.id = t.user_id
        WHERE t.expires_at IS NOT NULL
          AND t.expires_at <= now() - make_interval(days => {POLICY_B_GRACE_DAYS})
          AND u.is_active = true
          AND u.account_type NOT IN ('admin', 'superadmin', 'demo')
        """
    )


def _policy_b_email_html(tier: str, deadline_local: str) -> str:
    return (
        '<!doctype html><html><body style="margin:0;padding:24px;background:#0f1310;'
        'font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#e8ece4">'
        '<div style="max-width:520px;margin:0 auto;background:#161b16;'
        'border:1px solid #2a322a;border-radius:12px;padding:32px">'
        '<h1 style="margin:0 0 16px;font-size:20px;font-weight:600;color:#ffffff">'
        'Your Aivory subscription has lapsed</h1>'
        '<p style="margin:0 0 12px;font-size:14px;line-height:1.6;color:#c3cbbc">'
        "Your " + tier + " subscription wasn't renewed. You still have access "
        "to renew it, but if it's not renewed by " + deadline_local + " your "
        "account and its data will be removed.</p>"
        '<p style="margin:0 0 24px">'
        '<a href="https://aivory.uk/pricing" style="display:inline-block;'
        'background:#b2cca2;color:#0f1310;text-decoration:none;font-weight:600;'
        'font-size:14px;padding:12px 24px;border-radius:8px">Renew your subscription</a></p>'
        '<p style="margin:0;font-size:12px;line-height:1.6;color:#7d8878">'
        "If you meant to cancel, no action is needed.</p>"
        '</div></body></html>'
    )


async def _send_policy_b_warning(email: str, tier: str, expires_at: datetime) -> bool:
    deadline = expires_at + timedelta(days=POLICY_B_GRACE_DAYS)
    deadline_local = deadline.strftime("%Y-%m-%d %H:%M UTC")
    tier_label = (tier or "").strip() or "paid"
    text = (
        f"Your {tier_label} subscription wasn't renewed. Renew by "
        f"{deadline_local} to keep your account — after that it and its data "
        "are removed.\n\nhttps://aivory.uk/pricing"
    )
    return await email_service.send_email(
        email,
        "Your Aivory subscription has lapsed",
        _policy_b_email_html(tier_label, deadline_local),
        text,
    )


# ---------------------------------------------------------------------------
# Hard delete
# ---------------------------------------------------------------------------

async def _hard_delete(conn, user_id: str, email: str, account_type: str, created_at, reason: str) -> None:
    """
    One transaction: archive, clear the non-cascading FK references, delete.

    `sessions` and `password_reset_tokens` cascade automatically. `billing.*`
    rows (payment_orders, user_tiers, notifications, user_credits) are
    deliberately left in place as orphaned historical/financial records —
    deleting a customer shouldn't erase the transaction ledger.
    """
    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO deleted_users_archive (id, email, account_type, created_at, reason)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (id) DO NOTHING
            """,
            user_id, email, account_type, created_at, reason,
        )
        # Two audit_logs/impersonation_sessions tables exist (identity.* and
        # audit.*, historical schema drift) — both FK to users with NO ACTION,
        # so both must be cleared or the DELETE below fails.
        for schema in ("identity", "audit"):
            await conn.execute(f"DELETE FROM {schema}.audit_logs WHERE user_id = $1", user_id)
            await conn.execute(
                f"DELETE FROM {schema}.impersonation_sessions "
                f"WHERE admin_user_id = $1 OR target_user_id = $1",
                user_id,
            )
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------

async def _tick() -> None:
    if not await pg.is_available():
        return
    pool = await pg.get_pool()
    dry_run = not settings.account_cleanup_enabled

    # Policy A warnings
    for row in await _policy_a_warn_candidates(pool):
        deadline = row["created_at"] + timedelta(hours=POLICY_A_DELETE_AFTER_HOURS)
        if dry_run:
            logger.info("[cleanup dry-run] would warn (policy A) %s, deadline %s", row["email"], deadline)
            continue
        await _send_policy_a_warning(row["email"], deadline)
        await pool.execute("UPDATE users SET cleanup_warned_at = now() WHERE id = $1", row["id"])
        logger.info("Sent policy-A cleanup warning to %s", row["email"])

    # Policy A deletions
    for row in await _policy_a_delete_candidates(pool):
        if dry_run:
            logger.info("[cleanup dry-run] would hard-delete (policy A, never purchased) %s", row["email"])
            continue
        async with pool.acquire() as conn:
            await _hard_delete(
                conn, row["id"], row["email"], row["account_type"], row["created_at"],
                reason="policy_a_no_purchase_32h",
            )
        logger.info("Hard-deleted %s (policy A: no purchase within 32h)", row["email"])

    # Policy B warnings
    for row in await _policy_b_warn_candidates(pool):
        if dry_run:
            logger.info("[cleanup dry-run] would warn (policy B) %s, tier=%s", row["email"], row["tier"])
            continue
        await _send_policy_b_warning(row["email"], row["tier"], row["expires_at"])
        await pool.execute(
            "UPDATE user_tiers SET lapse_warned_at = now() WHERE user_id = $1", row["user_id"]
        )
        logger.info("Sent policy-B lapse warning to %s", row["email"])

    # Policy B deletions
    for row in await _policy_b_delete_candidates(pool):
        if dry_run:
            logger.info("[cleanup dry-run] would hard-delete (policy B, lapsed 40d+) %s", row["email"])
            continue
        async with pool.acquire() as conn:
            await _hard_delete(
                conn, row["user_id"], row["email"], row["account_type"], row["created_at"],
                reason="policy_b_subscription_lapsed_40d",
            )
        logger.info("Hard-deleted %s (policy B: subscription lapsed 40d+)", row["email"])


async def run_poller() -> None:
    """Started once from main.py's lifespan; never returns."""
    logger.info(
        "Account-cleanup poller started (enabled=%s, interval=%ds)",
        settings.account_cleanup_enabled, settings.account_cleanup_interval_seconds,
    )
    while True:
        try:
            await _tick()
        except Exception as e:
            logger.error("Account-cleanup tick failed: %s", e)
        await asyncio.sleep(settings.account_cleanup_interval_seconds)

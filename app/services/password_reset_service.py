"""
Self-service password resets.

Design notes worth keeping:

* Only `sha256(token)` is stored. The link we mail is a bearer credential for
  the whole account, so a database dump must not be replayable.
* `request_reset` never reveals whether an address exists — the route returns
  the same body either way. Anything else turns the endpoint into an account
  enumeration oracle.
* Consuming a token invalidates every *other* outstanding token for that user
  and drops their sessions, so a reset actually locks out whoever prompted it.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import bcrypt

from app.config import settings
from app.database import pg_service as pg
from app.services import email_service

logger = logging.getLogger(__name__)

# Audiences get different links (marketing site vs admin dashboard) but share
# one token table; the audience is recorded so the reset page knows where the
# user belongs and the audit trail shows which surface issued the link.
USER_AUDIENCE = "user"
ADMIN_AUDIENCE = "admin"

ADMIN_ACCOUNT_TYPES = ("admin", "superadmin")

# Cap on live tokens per account. Without it, repeatedly hitting
# "forgot password" mints unlimited valid links to the same inbox.
MAX_ACTIVE_TOKENS = 3

MIN_PASSWORD_LENGTH = 8


class ResetAborted(Exception):
    """Aborts the reset transaction with a user-facing message."""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _reset_url(token: str, audience: str) -> str:
    base = (
        settings.admin_password_reset_url_base
        if audience == ADMIN_AUDIENCE
        else settings.password_reset_url_base
    )
    return base.rstrip("/") + "?token=" + token


def audience_for(account_type: Optional[str]) -> str:
    """Which surface a given account resets its password on."""
    return ADMIN_AUDIENCE if account_type in ADMIN_ACCOUNT_TYPES else USER_AUDIENCE


def validate_password(password: Optional[str]) -> Optional[str]:
    """Return an error message, or None when the password is acceptable."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return "Password must be at least %d characters long" % MIN_PASSWORD_LENGTH
    if len(password) > 200:
        return "Password must be at most 200 characters long"
    return None


async def request_reset(
    email: str,
    audience: Optional[str] = None,
    requested_ip: Optional[str] = None,
) -> bool:
    """
    Mint a reset token for `email` and mail the link.

    Returns True when a mail actually went out — for logging only. Callers must
    respond identically regardless, or they leak which addresses are registered.
    """
    email = (email or "").strip().lower()
    if not email:
        return False

    user = await pg.get_user_by_email(email)
    if not user:
        logger.info("Password reset requested for an unknown address")
        return False
    if not user.get("is_active", True):
        logger.info("Password reset requested for deactivated user %s", user["id"])
        return False

    account_audience = audience_for(user.get("account_type"))
    # An admin asking through the public site should still get the admin link:
    # the account decides the surface, not the form that was submitted.
    if audience and audience != account_audience:
        logger.info(
            "Reset requested via the %s form for a %s account; sending the %s link",
            audience, user.get("account_type"), account_audience,
        )
    audience = account_audience

    pool = await pg.get_pool()

    # Retire the oldest live tokens once the cap is reached, rather than
    # refusing the request — someone who never received the first mail must
    # still be able to ask again.
    await pool.execute(
        """
        UPDATE password_reset_tokens
           SET used_at = NOW()
         WHERE user_id = $1
           AND used_at IS NULL
           AND expires_at > NOW()
           AND id NOT IN (
               SELECT id FROM password_reset_tokens
                WHERE user_id = $1 AND used_at IS NULL AND expires_at > NOW()
                ORDER BY created_at DESC
                LIMIT $2
           )
        """,
        user["id"], MAX_ACTIVE_TOKENS - 1,
    )

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.password_reset_ttl_minutes
    )
    await pool.execute(
        """
        INSERT INTO password_reset_tokens
            (user_id, token_hash, audience, expires_at, requested_ip)
        VALUES ($1, $2, $3, $4, $5)
        """,
        user["id"], _hash_token(token), audience, expires_at, requested_ip,
    )

    link = _reset_url(token, audience)
    minutes = settings.password_reset_ttl_minutes
    name = user.get("full_name") or user.get("username") or "there"

    text = (
        "Hi " + str(name) + ",\n\n"
        "We received a request to reset the password for your Aivory account.\n"
        "Open this link to choose a new one (it expires in "
        + str(minutes) + " minutes):\n\n"
        + link + "\n\n"
        "If you didn't ask for this, you can ignore this email - your password "
        "stays as it is.\n\n"
        "- Aivory"
    )
    html = _reset_email_html(name=str(name), link=link, minutes=minutes)

    sent = await email_service.send_email(
        email, "Reset your Aivory password", html, text
    )
    if not sent:
        # The token is still valid; an admin can re-issue or set one directly.
        logger.warning(
            "Reset token minted for %s but the email did not go out", user["id"]
        )
    return sent


def _reset_email_html(name: str, link: str, minutes: int) -> str:
    """The reset mail body. Inline styles only — mail clients strip <style>."""
    return (
        '<!doctype html><html><body style="margin:0;padding:24px;background:#0f1310;'
        'font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#e8ece4">'
        '<div style="max-width:520px;margin:0 auto;background:#161b16;'
        'border:1px solid #2a322a;border-radius:12px;padding:32px">'
        '<h1 style="margin:0 0 16px;font-size:20px;font-weight:600;color:#ffffff">'
        'Reset your password</h1>'
        '<p style="margin:0 0 12px;font-size:14px;line-height:1.6;color:#c3cbbc">'
        'Hi ' + name + ',</p>'
        '<p style="margin:0 0 24px;font-size:14px;line-height:1.6;color:#c3cbbc">'
        'We received a request to reset the password for your Aivory account. '
        'Choose a new one using the button below &mdash; the link expires in '
        + str(minutes) + ' minutes.</p>'
        '<p style="margin:0 0 24px">'
        '<a href="' + link + '" style="display:inline-block;background:#b2cca2;'
        'color:#0f1310;text-decoration:none;font-weight:600;font-size:14px;'
        'padding:12px 24px;border-radius:8px">Choose a new password</a></p>'
        '<p style="margin:0 0 8px;font-size:12px;line-height:1.6;color:#7d8878">'
        'If the button does not work, paste this into your browser:</p>'
        '<p style="margin:0 0 24px;font-size:12px;line-height:1.6;color:#7d8878;'
        'word-break:break-all">' + link + '</p>'
        '<p style="margin:0;font-size:12px;line-height:1.6;color:#7d8878">'
        'If you did not ask for this, you can ignore this email &mdash; your '
        'password stays as it is.</p>'
        '</div></body></html>'
    )


async def peek(token: str) -> Optional[dict]:
    """
    Look up a token without consuming it, so the reset page can say
    "this link has expired" before the user types a new password.
    """
    if not token:
        return None
    pool = await pg.get_pool()
    row = await pool.fetchrow(
        """
        SELECT t.id, t.user_id, t.audience, t.expires_at, t.used_at,
               u.email, u.is_active
          FROM password_reset_tokens t
          JOIN users u ON u.id = t.user_id
         WHERE t.token_hash = $1
        """,
        _hash_token(token),
    )
    if not row:
        return None
    if row["used_at"] is not None:
        return None
    if row["expires_at"] <= datetime.now(timezone.utc):
        return None
    if not row["is_active"]:
        return None
    return dict(row)


async def consume(token: str, new_password: str) -> Tuple[bool, str]:
    """
    Redeem `token` and set `new_password`. Returns (ok, message).

    The token row is claimed with a conditional UPDATE so two concurrent
    submissions of the same link cannot both succeed.
    """
    error = validate_password(new_password)
    if error:
        return False, error
    if not token:
        return False, "This reset link is invalid or has expired"

    pool = await pg.get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE password_reset_tokens
                       SET used_at = NOW()
                     WHERE token_hash = $1
                       AND used_at IS NULL
                       AND expires_at > NOW()
                    RETURNING id, user_id
                    """,
                    _hash_token(token),
                )
                if not row:
                    return False, "This reset link is invalid or has expired"

                user_id = row["user_id"]
                password_hash = bcrypt.hashpw(
                    new_password.encode(), bcrypt.gensalt(12)
                ).decode()
                result = await conn.execute(
                    "UPDATE users SET password_hash = $1, updated_at = NOW() "
                    "WHERE id = $2 AND is_active = true",
                    password_hash, user_id,
                )
                if result == "UPDATE 0":
                    # Deactivated between minting and redeeming: roll the whole
                    # thing back rather than burn the token on a no-op.
                    raise ResetAborted("This account is no longer active")

                # Any other live link for this account, and every existing
                # session, dies with the reset — otherwise whoever triggered it
                # keeps their access.
                await conn.execute(
                    "UPDATE password_reset_tokens SET used_at = NOW() "
                    "WHERE user_id = $1 AND used_at IS NULL",
                    user_id,
                )
                await conn.execute(
                    "DELETE FROM sessions WHERE user_id = $1", user_id
                )
    except ResetAborted as e:
        return False, str(e)

    logger.info("Password reset completed for %s", user_id)
    return True, "Password updated"


async def invalidate_all(user_id: str) -> None:
    """
    Kill every outstanding reset link for a user. Called when an admin sets a
    password directly, so an older emailed link cannot undo that.
    """
    pool = await pg.get_pool()
    await pool.execute(
        "UPDATE password_reset_tokens SET used_at = NOW() "
        "WHERE user_id = $1 AND used_at IS NULL",
        user_id,
    )

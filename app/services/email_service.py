"""
Outbound email for account/security mail (password resets).

Deliberately thin and failure-tolerant: it reuses the same SMTP_* settings the
rest of the stack already has configured for its third-party relay, and returns
False instead of raising. A dead mail server must never turn into a 500 on
`POST /auth/forgot-password` — that would both break the page and hand an
attacker a way to tell which addresses exist.
"""

import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    """True when there is an SMTP host to talk to."""
    return bool(settings.smtp_host)


def sender() -> Optional[str]:
    """The From address for account mail."""
    return settings.auth_from_email or settings.smtp_from_email


async def send_email(to_address: str, subject: str, html: str, text: str) -> bool:
    """
    Send one email. Returns False (never raises) if it could not be delivered.
    """
    if not is_configured() or not to_address:
        logger.warning(
            "Skipping email '%s' to %s: SMTP is not configured", subject, to_address
        )
        return False

    try:
        import aiosmtplib
        from email.message import EmailMessage

        message = EmailMessage()
        message["From"] = sender()
        message["To"] = to_address
        message["Subject"] = subject
        message.set_content(text)
        message.add_alternative(html, subtype="html")

        # 587 is STARTTLS, 465 is implicit TLS. Getting this backwards hangs
        # the connection until the timeout rather than failing cleanly.
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user or None,
            password=settings.smtp_password or None,
            start_tls=settings.smtp_port == 587,
            use_tls=settings.smtp_port == 465,
            timeout=20,
        )
        logger.info("Sent '%s' to %s", subject, to_address)
        return True
    except Exception as e:
        logger.warning("Could not email %s (%s): %s", to_address, subject, e)
        return False

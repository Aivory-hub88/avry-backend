"""
Cloudflare Turnstile check for public account creation.

Free accounts get real LLM work (2 diagnostics + 1 Copilot session), so
POST /api/v1/auth/register must not be scriptable. The landing renders the
widget with action "signup" and sends its token as `turnstile_token`; this
module exchanges it with Cloudflare before the account is created.

Mode (env REGISTER_TURNSTILE):
  enforce  every public register needs a valid token (default when
           TURNSTILE_SECRET is set)
  off      no check (default when TURNSTILE_SECRET is unset; also the
           rollback switch)

Trusted server-to-server callers that present X-Internal-Token skip the
check. Everything else fails closed: missing secret in enforce mode,
network error, wrong action, unknown hostname.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
EXPECTED_ACTION = "signup"
MAX_TOKEN_LENGTH = 2048


class TurnstileRejected(Exception):
    """The request did not prove a human; the account must not be created."""


class TurnstileUnavailable(Exception):
    """Enforcement is on but the server cannot verify (misconfigured)."""


def mode() -> str:
    explicit = (os.getenv("REGISTER_TURNSTILE") or "").strip().lower()
    if explicit in ("enforce", "off"):
        return explicit
    return "enforce" if os.getenv("TURNSTILE_SECRET") else "off"


def _hostnames() -> set[str]:
    return {h.strip() for h in (os.getenv("TURNSTILE_HOSTNAMES") or "").split(",") if h.strip()}


def is_internal_caller(x_internal_token: Optional[str]) -> bool:
    expected = os.getenv("INTERNAL_SERVICE_TOKEN") or os.getenv("TELEGRAM_GATEWAY_TOKEN")
    return bool(expected and x_internal_token and secrets.compare_digest(x_internal_token, expected))


async def verify_signup(
    token: Optional[str],
    *,
    remote_ip: Optional[str] = None,
    x_internal_token: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> None:
    """Return when the register may proceed; raise otherwise."""
    if mode() == "off" or is_internal_caller(x_internal_token):
        return

    secret = os.getenv("TURNSTILE_SECRET")
    hostnames = _hostnames()
    if not secret or not hostnames:
        logger.error("[turnstile] enforce mode without TURNSTILE_SECRET/TURNSTILE_HOSTNAMES")
        raise TurnstileUnavailable()

    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
        raise TurnstileRejected("missing token")

    data = {"secret": secret, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip

    try:
        if client is None:
            async with httpx.AsyncClient(timeout=10.0) as c:
                resp = await c.post(SITEVERIFY_URL, data=data)
        else:
            resp = await client.post(SITEVERIFY_URL, data=data)
        resp.raise_for_status()
        result = resp.json()
    except Exception as exc:  # network, non-2xx, bad JSON
        logger.error(f"[turnstile] siteverify failed: {exc}")
        raise TurnstileRejected("siteverify unavailable") from exc

    if (
        result.get("success") is not True
        or result.get("action") != EXPECTED_ACTION
        or result.get("hostname") not in hostnames
    ):
        logger.warning(
            "[turnstile] rejected success=%s action=%s hostname=%s errors=%s",
            result.get("success"),
            result.get("action"),
            result.get("hostname"),
            result.get("error-codes"),
        )
        raise TurnstileRejected("not verified")

"""
Security middleware — fixes pentest findings:
- A03-SQLi: Suppress DB error details in responses
- A08-Deser: Block prototype pollution (__proto__, constructor)
- A07-BruteForce: Rate limit login attempts
"""

import json
import time
import logging
from collections import defaultdict
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


# ── Prototype Pollution Guard ────────────────────────────────────────────────

DANGEROUS_KEYS = frozenset([
    "__proto__",
    "constructor",
    "prototype",
    "__defineGetter__",
    "__defineSetter__",
    "__lookupGetter__",
    "__lookupSetter__",
])


def _contains_dangerous_keys(obj, depth=0):
    """Recursively check for prototype pollution keys in parsed JSON."""
    if depth > 10:
        return False
    if isinstance(obj, dict):
        for key in obj:
            if key in DANGEROUS_KEYS:
                return True
            if _contains_dangerous_keys(obj[key], depth + 1):
                return True
    elif isinstance(obj, list):
        for item in obj:
            if _contains_dangerous_keys(item, depth + 1):
                return True
    return False


class PrototypePollutionGuard(BaseHTTPMiddleware):
    """Block requests containing __proto__, constructor, or prototype keys."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method in ("POST", "PUT", "PATCH"):
            content_type = request.headers.get("content-type", "")
            if "application/json" in content_type:
                body = await request.body()

                # Re-inject the consumed body so downstream handlers can read it.
                # Reading request.body() inside a BaseHTTPMiddleware drains the ASGI
                # receive stream; without replaying it the route handler hangs forever
                # waiting for a body that never arrives (broke every JSON POST/PUT/PATCH,
                # e.g. /api/v1/auth/login).
                async def _replay_receive():
                    return {"type": "http.request", "body": body, "more_body": False}
                request._receive = _replay_receive

                if body:
                    try:
                        parsed = json.loads(body)
                        if _contains_dangerous_keys(parsed):
                            logger.warning(
                                f"Prototype pollution attempt blocked: "
                                f"{request.client.host} {request.method} {request.url.path}"
                            )
                            return JSONResponse(
                                status_code=400,
                                content={
                                    "error": "invalid_input",
                                    "message": "Request contains disallowed property names",
                                },
                            )
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass  # Let downstream handle malformed JSON
                    except Exception:
                        pass

        return await call_next(request)


# ── Login Rate Limiter ───────────────────────────────────────────────────────

class LoginRateLimiter(BaseHTTPMiddleware):
    """
    Rate limit login attempts per IP address.
    Max 5 attempts per 15 minutes. After that, 429 Too Many Requests.
    """

    def __init__(self, app, max_attempts: int = 5, window_seconds: int = 900):
        super().__init__(app)
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        # {ip: [(timestamp, ...),]}
        self._attempts: dict = defaultdict(list)

    def _cleanup_old(self, ip: str):
        """Remove attempts older than the window."""
        cutoff = time.time() - self.window_seconds
        self._attempts[ip] = [t for t in self._attempts[ip] if t > cutoff]

    async def dispatch(self, request: Request, call_next) -> Response:
        # Only rate-limit the login endpoint
        if request.url.path == "/api/v1/auth/login" and request.method == "POST":
            ip = request.client.host if request.client else "unknown"
            self._cleanup_old(ip)

            if len(self._attempts[ip]) >= self.max_attempts:
                remaining = int(
                    self.window_seconds - (time.time() - self._attempts[ip][0])
                )
                logger.warning(f"Login rate limit exceeded for IP: {ip}")
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": "rate_limited",
                        "message": "Too many login attempts. Please try again later.",
                        "retry_after_seconds": remaining,
                    },
                    headers={"Retry-After": str(remaining)},
                )

            self._attempts[ip].append(time.time())

        return await call_next(request)


# ── Global Exception Handler ─────────────────────────────────────────────────

class ErrorSanitizer(BaseHTTPMiddleware):
    """
    Catch unhandled exceptions and return generic error messages.
    Prevents SQL errors, stack traces, and internal paths from leaking.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        try:
            response = await call_next(request)
            return response
        except Exception as e:
            logger.error(
                f"Unhandled exception on {request.method} {request.url.path}: {e}",
                exc_info=True,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": "internal_error",
                    "message": "An unexpected error occurred. Please try again later.",
                },
            )

"""
Impersonation Middleware — Detects and validates impersonation context on every request.

Responsibilities:
- Detect `impersonation_token` cookie on incoming requests
- Validate token via ImpersonationTokenService
- If invalid/expired: clear cookie, return 401
- If valid: inject impersonation context into request.state
- Set request.state.effective_user_id = target_user_id for downstream data queries
- Preserve admin_user_id for audit attribution
- Enforce access mode restrictions (block mutations in read-only mode)
- Block always-forbidden operations (password change, account deletion, payment modification, role changes)
- Trigger per-request audit logging
- Terminate session if audit logging fails
"""

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.services.impersonation_audit_logger import ImpersonationAuditLogger
from app.services.impersonation_session_manager import ImpersonationSessionManager
from app.services.impersonation_token_service import ImpersonationTokenService

logger = logging.getLogger(__name__)

IMPERSONATION_COOKIE_NAME = "impersonation_token"
IMPERSONATION_COOKIE_DOMAIN = ".avry.io"

# HTTP methods considered mutating
MUTATING_METHODS = frozenset(["POST", "PUT", "PATCH", "DELETE"])

# Paths exempt from access mode enforcement (impersonation lifecycle endpoints)
EXEMPT_PATHS = frozenset([
    "/api/v1/impersonation/end",
    "/api/v1/impersonation/extend",
    "/api/v1/impersonation/status",
])

# Always-forbidden endpoint patterns (blocked in ANY access mode)
# Each tuple: (path_substring, frozenset_of_blocked_methods)
ALWAYS_FORBIDDEN_PATTERNS = [
    ("/password", frozenset(["PUT", "POST", "PATCH"])),
    ("/account", frozenset(["DELETE"])),
    ("/user", frozenset(["DELETE"])),
    ("/payment", frozenset(["POST", "PUT", "PATCH", "DELETE"])),
    ("/role", frozenset(["POST", "PUT", "PATCH", "DELETE"])),
]


@dataclass
class ImpersonationContext:
    """Impersonation context injected into request.state.impersonation."""
    admin_user_id: str
    target_user_id: str
    access_mode: str
    session_id: str


class ImpersonationMiddleware(BaseHTTPMiddleware):
    """
    FastAPI middleware that intercepts every request to detect and enforce
    impersonation context via the `impersonation_token` cookie.
    """

    def __init__(self, app):
        super().__init__(app)
        self.token_service = ImpersonationTokenService()
        self.audit_logger = ImpersonationAuditLogger()
        self.session_manager = ImpersonationSessionManager()

    def _is_always_forbidden(self, path: str, method: str) -> bool:
        """
        Check if the request matches an always-forbidden endpoint pattern.

        Always-forbidden operations (blocked in ANY access mode):
        - Password changes: endpoints containing "/password" with PUT/POST/PATCH
        - Account deletion: endpoints containing "/account" or "/user" with DELETE
        - Payment modifications: endpoints containing "/payment" with POST/PUT/PATCH/DELETE
        - Role changes: endpoints containing "/role" with POST/PUT/PATCH/DELETE
        """
        path_lower = path.lower()
        for pattern, blocked_methods in ALWAYS_FORBIDDEN_PATTERNS:
            if pattern in path_lower and method in blocked_methods:
                return True
        return False

    async def dispatch(self, request: Request, call_next) -> Response:
        """
        Process each incoming request:
        1. Check for impersonation_token cookie
        2. If absent, pass through normally (no impersonation)
        3. If present, validate the token
        4. If invalid/expired, clear cookie and return 401
        5. If valid, inject impersonation context into request.state
        6. Enforce access mode restrictions
        7. Trigger per-request audit logging
        """
        # Initialize request.state defaults
        request.state.impersonation = None
        request.state.effective_user_id = None

        # 1. Detect impersonation token cookie
        token = request.cookies.get(IMPERSONATION_COOKIE_NAME)

        if not token:
            # No impersonation — pass through normally
            return await call_next(request)

        # 2. Validate the token
        payload = self.token_service.validate_token(token)

        if payload is None:
            # Token is invalid or expired — clear cookie and return 401
            logger.warning(
                f"Invalid/expired impersonation token detected from "
                f"{request.client.host if request.client else 'unknown'} "
                f"on {request.method} {request.url.path}"
            )
            response = JSONResponse(
                status_code=401,
                content={
                    "error": "invalid_token",
                    "message": "Impersonation token is invalid or expired",
                },
            )
            # Clear the invalid cookie
            response.delete_cookie(
                key=IMPERSONATION_COOKIE_NAME,
                path="/",
                domain=IMPERSONATION_COOKIE_DOMAIN,
            )
            return response

        # 3. Token is valid — inject impersonation context into request.state
        impersonation_context = ImpersonationContext(
            admin_user_id=payload.admin_user_id,
            target_user_id=payload.target_user_id,
            access_mode=payload.access_mode,
            session_id=payload.session_id,
        )

        request.state.impersonation = impersonation_context

        # 4. Set effective_user_id to target_user_id for downstream data queries
        request.state.effective_user_id = payload.target_user_id

        logger.debug(
            f"Impersonation active: admin={payload.admin_user_id} -> "
            f"target={payload.target_user_id} | mode={payload.access_mode} | "
            f"session={payload.session_id} | {request.method} {request.url.path}"
        )

        # 5. Access mode enforcement
        method = request.method
        path = request.url.path

        # 5.0 Skip enforcement for impersonation lifecycle endpoints
        # (admin must be able to end/extend session even in read-only mode)
        if path in EXEMPT_PATHS:
            start_time = time.time()
            response = await call_next(request)
            response_time_ms = (time.time() - start_time) * 1000

            # Still log the request for audit purposes
            admin_ip = request.client.host if request.client else "unknown"
            admin_user_agent = request.headers.get("user-agent", "unknown")

            await self.audit_logger.log_request(
                session_id=payload.session_id,
                admin_user_id=payload.admin_user_id,
                target_user_id=payload.target_user_id,
                endpoint=path,
                method=method,
                path=path,
                query_params=dict(request.query_params) if request.query_params else {},
                body_hash=None,
                status_code=response.status_code,
                response_time_ms=round(response_time_ms, 2),
                admin_ip=admin_ip,
                admin_user_agent=admin_user_agent,
            )
            return response

        # 5a. Check always-forbidden operations (blocked in ANY mode)
        if self._is_always_forbidden(path, method):
            # Log the blocked mutation attempt
            await self.audit_logger.log_mutation_blocked(
                session_id=payload.session_id,
                admin_user_id=payload.admin_user_id,
                target_user_id=payload.target_user_id,
                endpoint=path,
                method=method,
                reason="always_forbidden_operation",
            )
            logger.warning(
                f"Always-forbidden operation blocked: "
                f"admin={payload.admin_user_id} | session={payload.session_id} | "
                f"{method} {path}"
            )
            return JSONResponse(
                status_code=403,
                content={
                    "error": "operation_forbidden",
                    "message": "This operation is never permitted during impersonation",
                },
            )

        # 5b. Read-only mode: block all mutating requests
        if payload.access_mode == "read_only" and method in MUTATING_METHODS:
            # Log the blocked mutation attempt
            await self.audit_logger.log_mutation_blocked(
                session_id=payload.session_id,
                admin_user_id=payload.admin_user_id,
                target_user_id=payload.target_user_id,
                endpoint=path,
                method=method,
                reason="read_only_mode",
            )
            logger.info(
                f"Read-only mutation blocked: "
                f"admin={payload.admin_user_id} | session={payload.session_id} | "
                f"{method} {path}"
            )
            return JSONResponse(
                status_code=403,
                content={
                    "error": "read_only",
                    "message": "This action is not permitted in read-only impersonation mode",
                },
            )

        # 6. Process the request and measure response time
        start_time = time.time()
        response = await call_next(request)
        response_time_ms = (time.time() - start_time) * 1000

        # 7. Per-request audit logging
        # Compute body hash for audit (if body present)
        body_hash = None
        request_body = None
        if method in MUTATING_METHODS:
            try:
                body_bytes = await request.body()
                if body_bytes:
                    body_hash = hashlib.sha256(body_bytes).hexdigest()
                    # For full-access mutations, try to parse the body for audit
                    try:
                        import json
                        request_body = json.loads(body_bytes)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        request_body = None
            except Exception:
                pass

        admin_ip = request.client.host if request.client else "unknown"
        admin_user_agent = request.headers.get("user-agent", "unknown")

        audit_success = await self.audit_logger.log_request(
            session_id=payload.session_id,
            admin_user_id=payload.admin_user_id,
            target_user_id=payload.target_user_id,
            endpoint=path,
            method=method,
            path=path,
            query_params=dict(request.query_params) if request.query_params else {},
            body_hash=body_hash,
            status_code=response.status_code,
            response_time_ms=round(response_time_ms, 2),
            admin_ip=admin_ip,
            admin_user_agent=admin_user_agent,
            request_body=request_body,
            access_mode=payload.access_mode,
        )

        # 8. If audit log write fails, terminate session immediately
        if not audit_success:
            logger.error(
                f"Audit log write failed — terminating impersonation session: "
                f"session={payload.session_id} | admin={payload.admin_user_id}"
            )
            # Terminate the session
            await self.session_manager.end_session(
                session_id=payload.session_id,
                reason="audit_failure",
            )
            # Return error response with cleared cookie
            error_response = JSONResponse(
                status_code=500,
                content={
                    "error": "audit_failure",
                    "message": "Session terminated due to logging failure",
                },
            )
            error_response.delete_cookie(
                key=IMPERSONATION_COOKIE_NAME,
                path="/",
                domain=IMPERSONATION_COOKIE_DOMAIN,
            )
            return error_response

        return response

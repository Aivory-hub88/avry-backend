"""
Impersonation Audit Logger — Writes structured impersonation events to the PostgreSQL `audit_logs` table.

All impersonation events use:
    - action = "impersonation"
    - entity_type = "impersonation_session"
    - entity_id = session_id
    - changes = JSONB payload with sub_action field

The `changes` column carries the full structured event data including request context,
timing information, and session metadata.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional, List

from app.database import pg_service as pg
from app.events.impersonation_publisher import (
    publish_impersonation_event,
    publish_session_start_alert,
    IMPERSONATION_EVENTS_EXCHANGE,
)
from app.utils.id_generator import generate_audit_id

logger = logging.getLogger(__name__)

# Fields that must be sanitized from request bodies before audit logging
SENSITIVE_FIELDS = frozenset([
    "password",
    "password_hash",
    "payment_token",
    "card_number",
    "cvv",
    "secret_key",
])


def sanitize_request_body(body: Any) -> Any:
    """
    Sanitize a request body by replacing sensitive field values with "[REDACTED]".

    Recursively processes nested dictionaries. Sensitive fields are:
    password, password_hash, payment_token, card_number, cvv, secret_key.

    Args:
        body: The request body to sanitize. Typically a dict, but handles
              None, empty, and non-dict values gracefully.

    Returns:
        A sanitized copy of the body with sensitive values replaced by "[REDACTED]".
        Non-dict values (including None) are returned as-is.
    """
    if body is None:
        return None

    if not isinstance(body, dict):
        return body

    sanitized = {}
    for key, value in body.items():
        if key in SENSITIVE_FIELDS:
            sanitized[key] = "[REDACTED]"
        elif isinstance(value, dict):
            sanitized[key] = sanitize_request_body(value)
        elif isinstance(value, list):
            sanitized[key] = [
                sanitize_request_body(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            sanitized[key] = value

    return sanitized


class ImpersonationAuditLogger:
    """
    Specialized audit logger for impersonation events.

    Writes to the existing `audit_logs` table using the JSONB `changes` column
    for structured event data. Each method corresponds to a specific impersonation
    lifecycle event (session_start, request, mutation_blocked, session_end).
    """

    async def log_session_start(
        self,
        session,
        admin_ip: str,
        admin_user_agent: str,
    ) -> bool:
        """
        Log an impersonation session start event.

        Writes to PostgreSQL audit_logs AND publishes to both the
        `impersonation.events` and `admin.alerts` RabbitMQ exchanges.

        Args:
            session: ImpersonationSession dataclass with id, admin_user_id, target_user_id, access_mode.
            admin_ip: IP address of the admin initiating the session.
            admin_user_agent: User-Agent string of the admin's browser.

        Returns:
            True if the log entry was written successfully, False otherwise.
        """
        changes = {
            "sub_action": "session_start",
            "admin_user_id": session.admin_user_id,
            "target_user_id": session.target_user_id,
            "access_mode": session.access_mode,
            "session_id": session.id,
        }

        db_success = await self._write_audit_log(
            user_id=session.admin_user_id,
            entity_id=session.id,
            changes=changes,
            ip_address=admin_ip,
            user_agent=admin_user_agent,
        )

        # Publish to RabbitMQ (graceful — failures don't affect the return value)
        event_data = {
            **changes,
            "admin_ip": admin_ip,
            "admin_user_agent": admin_user_agent,
        }
        await self.publish_event("session_start", event_data)

        # Also publish to admin.alerts exchange for external notification integrations
        await self._publish_session_start_alert(event_data)

        return db_success

    async def log_request(
        self,
        session_id: str,
        admin_user_id: str,
        target_user_id: str,
        endpoint: str,
        method: str,
        path: str,
        query_params: Optional[dict],
        body_hash: Optional[str],
        status_code: int,
        response_time_ms: float,
        admin_ip: str,
        admin_user_agent: str,
        request_body: Optional[dict] = None,
        access_mode: Optional[str] = None,
    ) -> bool:
        """
        Log an API request made during an impersonation session.

        Args:
            session_id: The impersonation session ID.
            admin_user_id: The admin's user ID (for audit attribution).
            target_user_id: The target user's ID being impersonated.
            endpoint: The API endpoint that was accessed.
            method: HTTP method (GET, POST, PUT, PATCH, DELETE).
            path: The full request path.
            query_params: Query parameters dictionary (can be empty dict).
            body_hash: SHA-256 hash of the request body, or None for bodyless requests.
            status_code: HTTP response status code.
            response_time_ms: Time taken to process the request in milliseconds.
            admin_ip: IP address of the admin making the request.
            admin_user_agent: User-Agent string of the admin's browser.
            request_body: The request body dict for full-access mutations (will be sanitized before logging).
            access_mode: The session access mode ("read_only" or "full_access").

        Returns:
            True if the log entry was written successfully, False otherwise.
        """
        changes = {
            "sub_action": "request",
            "session_id": session_id,
            "admin_user_id": admin_user_id,
            "target_user_id": target_user_id,
            "endpoint": endpoint,
            "method": method,
            "request_path": path,
            "query_parameters": query_params if query_params is not None else {},
            "request_body_hash": body_hash,
            "response_status_code": status_code,
            "response_time_ms": response_time_ms,
        }

        # For full-access mutation requests, include the sanitized request body
        if (
            access_mode == "full_access"
            and method in ("POST", "PUT", "PATCH", "DELETE")
            and request_body is not None
        ):
            changes["request_body"] = sanitize_request_body(request_body)

        return await self._write_audit_log(
            user_id=admin_user_id,
            entity_id=session_id,
            changes=changes,
            ip_address=admin_ip,
            user_agent=admin_user_agent,
        )

    async def log_mutation_blocked(
        self,
        session_id: str,
        admin_user_id: str,
        target_user_id: str,
        endpoint: str,
        method: str,
        reason: str,
    ) -> bool:
        """
        Log a blocked mutation attempt during an impersonation session.

        Args:
            session_id: The impersonation session ID.
            admin_user_id: The admin's user ID.
            target_user_id: The target user's ID being impersonated.
            endpoint: The API endpoint that was blocked.
            method: HTTP method that was attempted (POST, PUT, PATCH, DELETE).
            reason: Reason the mutation was blocked (e.g., "read_only_mode", "always_forbidden_operation").

        Returns:
            True if the log entry was written successfully, False otherwise.
        """
        changes = {
            "sub_action": "mutation_blocked",
            "session_id": session_id,
            "admin_user_id": admin_user_id,
            "target_user_id": target_user_id,
            "endpoint": endpoint,
            "method": method,
            "reason": reason,
        }

        return await self._write_audit_log(
            user_id=admin_user_id,
            entity_id=session_id,
            changes=changes,
            ip_address=None,
            user_agent=None,
        )

    async def log_session_end(
        self,
        session_id: str,
        reason: str,
        duration_seconds: int,
        total_requests: int,
        mutations_attempted: int,
        mutations_blocked: int,
        pages_visited: Optional[List[str]] = None,
    ) -> bool:
        """
        Log an impersonation session end event.

        Args:
            session_id: The impersonation session ID.
            reason: Termination reason ("manual", "expired", "error").
            duration_seconds: Total session duration in seconds.
            total_requests: Total number of API requests made during the session.
            mutations_attempted: Total number of mutation requests attempted.
            mutations_blocked: Total number of mutations that were blocked.
            pages_visited: List of page paths visited during the session.

        Returns:
            True if the log entry was written successfully, False otherwise.
        """
        changes = {
            "sub_action": "session_end",
            "session_id": session_id,
            "termination_reason": reason,
            "duration_seconds": duration_seconds,
            "total_requests_made": total_requests,
            "total_mutations_attempted": mutations_attempted,
            "total_mutations_blocked": mutations_blocked,
            "pages_visited": pages_visited if pages_visited is not None else [],
        }

        return await self._write_audit_log(
            user_id=None,
            entity_id=session_id,
            changes=changes,
            ip_address=None,
            user_agent=None,
        )

    async def publish_event(self, event_type: str, event_data: dict) -> bool:
        """
        Publish an impersonation event to the RabbitMQ `impersonation.events` exchange.

        Runs the synchronous pika publish call in a thread executor to avoid
        blocking the async event loop. Handles RabbitMQ unavailability gracefully
        by logging a warning and returning False — never terminates the session.

        Args:
            event_type: The type of event (e.g., "session_start", "request",
                        "mutation_blocked", "session_end").
            event_data: The full event data dictionary to publish.

        Returns:
            True if the event was published successfully, False otherwise.
        """
        routing_key = f"impersonation.{event_type}"

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                publish_impersonation_event,
                IMPERSONATION_EVENTS_EXCHANGE,
                routing_key,
                event_type,
                event_data,
            )
            return result
        except Exception as e:
            logger.warning(
                f"RabbitMQ publish failed for impersonation event '{event_type}': {e}"
            )
            return False

    async def _publish_session_start_alert(self, event_data: dict) -> bool:
        """
        Publish a session start alert to the `admin.alerts` exchange.

        This enables external notification integrations to be notified when
        an impersonation session begins.

        Args:
            event_data: The session start event data dictionary.

        Returns:
            True if published successfully, False otherwise.
        """
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                publish_session_start_alert,
                event_data,
            )
            return result
        except Exception as e:
            logger.warning(
                f"RabbitMQ publish failed for admin.alerts session start: {e}"
            )
            return False

    async def _write_audit_log(
        self,
        user_id: Optional[str],
        entity_id: str,
        changes: dict,
        ip_address: Optional[str],
        user_agent: Optional[str],
    ) -> bool:
        """
        Internal method to write an audit log entry to the `audit_logs` table.

        Args:
            user_id: The user ID for attribution (admin_user_id).
            entity_id: The impersonation session ID.
            changes: JSONB payload with structured event data.
            ip_address: Admin's IP address.
            user_agent: Admin's User-Agent string.

        Returns:
            True if the entry was inserted, False on failure.
        """
        try:
            pool = await pg.get_pool()
            audit_id = generate_audit_id()

            await pool.execute(
                """
                INSERT INTO audit_logs (id, user_id, action, entity_type, entity_id, changes, ip_address, user_agent, created_at)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
                """,
                audit_id,
                user_id,
                "impersonation",
                "impersonation_session",
                entity_id,
                json.dumps(changes),
                ip_address,
                user_agent,
                datetime.now(timezone.utc),
            )

            logger.info(
                f"Audit log written: {changes.get('sub_action')} | "
                f"session={entity_id} | id={audit_id}"
            )
            return True

        except Exception as e:
            logger.error(
                f"Failed to write audit log: {changes.get('sub_action')} | "
                f"session={entity_id} | error={e}"
            )
            return False

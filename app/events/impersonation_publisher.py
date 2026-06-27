"""
RabbitMQ Event Publisher for Impersonation Events.

Publishes impersonation audit events to dedicated exchanges:
- `impersonation.events` (topic): All impersonation audit events for real-time monitoring
- `admin.alerts` (topic): Session start alerts for external notification integrations

Routing Keys:
- impersonation.session_start
- impersonation.request
- impersonation.mutation_blocked
- impersonation.session_end
- admin.impersonation_started

Follows the existing app/events/publisher.py pattern using synchronous pika.
Handles RabbitMQ unavailability gracefully (logs warning, does not terminate session).
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import pika

logger = logging.getLogger(__name__)

# Exchange names
IMPERSONATION_EVENTS_EXCHANGE = "impersonation.events"
ADMIN_ALERTS_EXCHANGE = "admin.alerts"

# Connection state
_connection: Optional[pika.BlockingConnection] = None
_channel: Optional[pika.adapters.blocking_connection.BlockingChannel] = None


def _get_rabbitmq_url() -> str:
    """Get RabbitMQ connection URL from environment."""
    return os.getenv("RABBITMQ_URL", "amqp://admin:admin123@localhost:5672/")


def _get_connection() -> pika.BlockingConnection:
    """Get or create RabbitMQ connection. Raises on failure."""
    global _connection
    if _connection is None or _connection.is_closed:
        rabbitmq_url = _get_rabbitmq_url()
        _connection = pika.BlockingConnection(
            pika.URLParameters(rabbitmq_url)
        )
        logger.info("Impersonation publisher: RabbitMQ connection established")
    return _connection


def _get_channel() -> pika.adapters.blocking_connection.BlockingChannel:
    """Get or create RabbitMQ channel. Raises on failure."""
    global _channel
    connection = _get_connection()
    if _channel is None or _channel.is_closed:
        _channel = connection.channel()
        # Declare the impersonation exchanges
        _channel.exchange_declare(
            exchange=IMPERSONATION_EVENTS_EXCHANGE,
            exchange_type="topic",
            durable=True,
        )
        _channel.exchange_declare(
            exchange=ADMIN_ALERTS_EXCHANGE,
            exchange_type="topic",
            durable=True,
        )
        logger.info(
            "Impersonation publisher: channel created, exchanges declared "
            f"({IMPERSONATION_EVENTS_EXCHANGE}, {ADMIN_ALERTS_EXCHANGE})"
        )
    return _channel


def publish_impersonation_event(
    exchange: str,
    routing_key: str,
    event_type: str,
    event_data: Dict[str, Any],
    event_id: Optional[str] = None,
) -> bool:
    """
    Publish an impersonation event to the specified RabbitMQ exchange.

    This function handles RabbitMQ unavailability gracefully by logging
    a warning and returning False — it never raises an exception.

    Args:
        exchange: Target exchange name.
        routing_key: Message routing key.
        event_type: Descriptive event type string.
        event_data: Event payload dictionary.
        event_id: Optional event ID (auto-generated if not provided).

    Returns:
        True if the event was published successfully, False otherwise.
    """
    try:
        channel = _get_channel()

        event_message = {
            "event_id": event_id or f"imp_evt_{datetime.now(timezone.utc).timestamp()}",
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": event_data,
        }

        channel.basic_publish(
            exchange=exchange,
            routing_key=routing_key,
            body=json.dumps(event_message),
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=pika.spec.PERSISTENT_DELIVERY_MODE,
                content_encoding="utf-8",
            ),
        )

        logger.info(
            f"Impersonation event published: {event_type} -> "
            f"{exchange}:{routing_key}"
        )
        return True

    except Exception as e:
        # Graceful degradation: log warning but do NOT propagate the error
        logger.warning(
            f"Failed to publish impersonation event '{event_type}' to "
            f"{exchange}:{routing_key}: {e}"
        )
        # Reset connection state so next attempt will reconnect
        _reset_connection()
        return False


def publish_session_start_alert(event_data: Dict[str, Any]) -> bool:
    """
    Publish a session start alert to the admin.alerts exchange.

    Called when an impersonation session begins, enabling external
    notification integrations.

    Args:
        event_data: Session start event data.

    Returns:
        True if published successfully, False otherwise.
    """
    return publish_impersonation_event(
        exchange=ADMIN_ALERTS_EXCHANGE,
        routing_key="admin.impersonation_started",
        event_type="admin.impersonation_started",
        event_data=event_data,
    )


def _reset_connection():
    """Reset connection state so next call attempts a fresh connection."""
    global _connection, _channel
    try:
        if _channel and not _channel.is_closed:
            _channel.close()
    except Exception:
        pass
    try:
        if _connection and not _connection.is_closed:
            _connection.close()
    except Exception:
        pass
    _connection = None
    _channel = None


def close_connection():
    """Close the impersonation publisher RabbitMQ connection."""
    _reset_connection()
    logger.info("Impersonation publisher: connection closed")

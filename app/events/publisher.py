"""
RabbitMQ Event Publisher for Backend Service (Phase 2)
Publishes events to other services

Events published:
- user.events:
  - user.created (when new user registers)
  - user.updated (when user profile updated)
  - user.deleted (when user deleted)
  - user.activated (when email verified)

- auth.events:
  - auth.login (successful login)
  - auth.logout (user logout)
  - auth.failed (login attempt failed)
  - auth.token_refreshed (JWT token refreshed)
"""

import os
import pika
import json
import logging
from datetime import datetime
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Connection pool
_connection = None
_channel = None


def get_connection():
    """Get or create RabbitMQ connection"""
    global _connection
    if _connection is None or _connection.is_closed:
        try:
            # Use environment variable or default RabbitMQ URL
            rabbitmq_url = os.getenv("RABBITMQ_URL", "amqp://admin:admin123@localhost:5672/")
            _connection = pika.BlockingConnection(
                pika.URLParameters(rabbitmq_url)
            )
            logger.info("✓ RabbitMQ connection established")
        except Exception as e:
            logger.error(f"✗ Failed to connect to RabbitMQ: {e}")
            raise
    return _connection


def get_channel():
    """Get or create RabbitMQ channel"""
    global _channel
    connection = get_connection()
    if _channel is None or _channel.is_closed:
        _channel = connection.channel()
        logger.info("✓ RabbitMQ channel created")
    return _channel


def ensure_exchange(exchange_name: str, exchange_type: str = 'topic'):
    """Ensure exchange exists"""
    try:
        channel = get_channel()
        channel.exchange_declare(
            exchange=exchange_name,
            exchange_type=exchange_type,
            durable=True
        )
        logger.debug(f"✓ Exchange '{exchange_name}' ready")
    except Exception as e:
        logger.error(f"✗ Failed to declare exchange '{exchange_name}': {e}")
        raise


def publish_event(
    exchange: str,
    routing_key: str,
    event_type: str,
    data: Dict[str, Any],
    event_id: Optional[str] = None
) -> bool:
    """
    Publish event to RabbitMQ
    
    Args:
        exchange: Exchange name (e.g., 'user.events')
        routing_key: Routing key (e.g., 'user.created')
        event_type: Type of event (e.g., 'user.created')
        data: Event payload data
        event_id: Optional event ID (auto-generated if not provided)
    
    Returns:
        True if published successfully, False otherwise
    """
    try:
        # Ensure exchange exists
        ensure_exchange(exchange)

        # Create event message
        event_message = {
            'event_id': event_id or f"evt_{datetime.now().timestamp()}",
            'event_type': event_type,
            'timestamp': datetime.now().isoformat(),
            'data': data
        }

        # Publish message
        channel = get_channel()
        channel.basic_publish(
            exchange=exchange,
            routing_key=routing_key,
            body=json.dumps(event_message),
            properties=pika.BasicProperties(
                content_type='application/json',
                delivery_mode=pika.spec.PERSISTENT_DELIVERY_MODE,  # Persistent
                content_encoding='utf-8'
            )
        )

        logger.info(f"📤 Published event: {event_type} to {exchange}:{routing_key}")
        return True

    except Exception as e:
        logger.error(f"✗ Failed to publish event '{event_type}': {e}")
        return False


# ============================================================================
# USER EVENTS
# ============================================================================

def publish_user_created(user_id: str, email: str, username: str, **kwargs) -> bool:
    """Publish user.created event"""
    return publish_event(
        exchange='user.events',
        routing_key='user.created',
        event_type='user.created',
        data={
            'user_id': user_id,
            'email': email,
            'username': username,
            **kwargs
        }
    )


def publish_user_updated(user_id: str, changes: Dict[str, Any]) -> bool:
    """Publish user.updated event"""
    return publish_event(
        exchange='user.events',
        routing_key='user.updated',
        event_type='user.updated',
        data={
            'user_id': user_id,
            'changes': changes
        }
    )


def publish_user_deleted(user_id: str, reason: Optional[str] = None) -> bool:
    """Publish user.deleted event"""
    return publish_event(
        exchange='user.events',
        routing_key='user.deleted',
        event_type='user.deleted',
        data={
            'user_id': user_id,
            'reason': reason
        }
    )


def publish_user_activated(user_id: str, email: str) -> bool:
    """Publish user.activated event (email verified)"""
    return publish_event(
        exchange='user.events',
        routing_key='user.activated',
        event_type='user.activated',
        data={
            'user_id': user_id,
            'email': email
        }
    )


# ============================================================================
# AUTH EVENTS
# ============================================================================

def publish_auth_login(user_id: str, ip_address: Optional[str] = None) -> bool:
    """Publish auth.login event"""
    return publish_event(
        exchange='auth.events',
        routing_key='auth.login',
        event_type='auth.login',
        data={
            'user_id': user_id,
            'ip_address': ip_address
        }
    )


def publish_auth_logout(user_id: str) -> bool:
    """Publish auth.logout event"""
    return publish_event(
        exchange='auth.events',
        routing_key='auth.logout',
        event_type='auth.logout',
        data={
            'user_id': user_id
        }
    )


def publish_auth_failed(email: str, reason: str, ip_address: Optional[str] = None) -> bool:
    """Publish auth.failed event"""
    return publish_event(
        exchange='auth.events',
        routing_key='auth.failed',
        event_type='auth.failed',
        data={
            'email': email,
            'reason': reason,
            'ip_address': ip_address
        }
    )


def publish_auth_token_refreshed(user_id: str) -> bool:
    """Publish auth.token_refreshed event"""
    return publish_event(
        exchange='auth.events',
        routing_key='auth.token_refreshed',
        event_type='auth.token_refreshed',
        data={
            'user_id': user_id
        }
    )


# ============================================================================
# SUBSCRIPTION EVENTS (FROM PAYMENTS SERVICE)
# ============================================================================

def publish_subscription_status_changed(user_id: str, status: str, plan: str) -> bool:
    """Publish subscription status change event"""
    return publish_event(
        exchange='user.events',
        routing_key='user.subscription_changed',
        event_type='user.subscription_changed',
        data={
            'user_id': user_id,
            'subscription_status': status,
            'plan': plan
        }
    )


# ============================================================================
# CLOSE CONNECTION
# ============================================================================

def close_connection():
    """Close RabbitMQ connection"""
    global _connection, _channel
    try:
        if _channel and not _channel.is_closed:
            _channel.close()
        if _connection and not _connection.is_closed:
            _connection.close()
        logger.info("✓ RabbitMQ connection closed")
    except Exception as e:
        logger.error(f"✗ Error closing RabbitMQ connection: {e}")


if __name__ == "__main__":
    # Test event publishing
    logging.basicConfig(level=logging.INFO)
    
    # Test user created event
    publish_user_created(
        user_id="test-user-123",
        email="test@example.com",
        username="testuser"
    )
    
    # Test auth login event
    publish_auth_login(
        user_id="test-user-123",
        ip_address="192.168.1.1"
    )
    
    logger.info("✓ Events published successfully")
    close_connection()

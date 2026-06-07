"""
RabbitMQ Event Consumer for Backend Service (Phase 2)
Consumes events from other services and updates local cache

Events consumed:
- payment.processed: When user makes a payment
- payment.failed: When payment fails
- subscription.created: When user creates subscription
- diagnostics.completed: When diagnostics run completes
"""

import os
import pika
import json
import logging
import redis
from datetime import datetime
from typing import Dict, Any
from threading import Thread
import time

logger = logging.getLogger(__name__)

# Redis client for caching
redis_client = redis.Redis(
    host='localhost',
    port=6379,
    db=0,
    decode_responses=True
)

class RabbitMQConsumer:
    def __init__(self, rabbitmq_url: str = None):
        # Use environment variable or provided URL or default
        if rabbitmq_url is None:
            rabbitmq_url = os.getenv("RABBITMQ_URL", "amqp://admin:admin123@localhost:5672/")
        self.rabbitmq_url = rabbitmq_url
        self.connection = None
        self.channel = None
        self.running = False

    def connect(self):
        """Establish connection to RabbitMQ"""
        try:
            self.connection = pika.BlockingConnection(
                pika.URLParameters(self.rabbitmq_url)
            )
            self.channel = self.connection.channel()
            logger.info("✓ Connected to RabbitMQ")
        except Exception as e:
            logger.error(f"✗ Failed to connect to RabbitMQ: {e}")
            raise

    def declare_exchanges_and_queues(self):
        """Declare exchanges and queues for backend service"""
        # Declare exchanges (from other services)
        self.channel.exchange_declare(
            exchange='payment.events',
            exchange_type='topic',
            durable=True
        )
        logger.info("✓ Declared payment.events exchange")

        self.channel.exchange_declare(
            exchange='diagnostics.events',
            exchange_type='topic',
            durable=True
        )
        logger.info("✓ Declared diagnostics.events exchange")

        # Declare queues
        self.channel.queue_declare(
            queue='backend_payment_events',
            durable=True
        )
        logger.info("✓ Declared backend_payment_events queue")

        self.channel.queue_declare(
            queue='backend_diagnostics_events',
            durable=True
        )
        logger.info("✓ Declared backend_diagnostics_events queue")

        # Bind queues to exchanges
        self.channel.queue_bind(
            exchange='payment.events',
            queue='backend_payment_events',
            routing_key='payment.*'
        )
        logger.info("✓ Bound payment.events to backend_payment_events")

        self.channel.queue_bind(
            exchange='diagnostics.events',
            queue='backend_diagnostics_events',
            routing_key='diagnostics.*'
        )
        logger.info("✓ Bound diagnostics.events to backend_diagnostics_events")

    def on_payment_event(self, ch, method, properties, body):
        """Handle payment events from payment service"""
        try:
            event = json.loads(body)
            user_id = event.get('user_id')
            status = event.get('status')
            amount = event.get('amount')

            logger.info(f"📨 Received payment event: {event.get('type')} for user {user_id}")

            # Cache payment status
            cache_key = f"user:payment_status:{user_id}"
            cache_data = {
                'user_id': user_id,
                'status': status,
                'amount': amount,
                'timestamp': datetime.now().isoformat()
            }
            redis_client.setex(cache_key, 3600, json.dumps(cache_data))  # 1 hour TTL

            # Also cache for quick lookup by subscription status
            if status == 'completed':
                redis_client.setex(f"user:has_active_subscription:{user_id}", 3600, "true")
            elif status == 'failed':
                redis_client.setex(f"user:has_active_subscription:{user_id}", 3600, "false")

            logger.info(f"✓ Cached payment event for user {user_id}")

            # Acknowledge message
            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error(f"✗ Error processing payment event: {e}")
            # Nack and requeue on error
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

    def on_diagnostics_event(self, ch, method, properties, body):
        """Handle diagnostics events from diagnostics service"""
        try:
            event = json.loads(body)
            user_id = event.get('user_id')
            status = event.get('status')
            results = event.get('results', {})

            logger.info(f"📨 Received diagnostics event: {event.get('type')} for user {user_id}")

            # Cache diagnostics results
            cache_key = f"user:diagnostics_results:{user_id}"
            cache_data = {
                'user_id': user_id,
                'status': status,
                'results': results,
                'timestamp': datetime.now().isoformat()
            }
            redis_client.setex(cache_key, 3600, json.dumps(cache_data))  # 1 hour TTL

            # Also cache completion status
            if status == 'completed':
                redis_client.setex(f"user:latest_diagnostics_status:{user_id}", 3600, "completed")
            elif status == 'failed':
                redis_client.setex(f"user:latest_diagnostics_status:{user_id}", 3600, "failed")

            logger.info(f"✓ Cached diagnostics event for user {user_id}")

            # Acknowledge message
            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error(f"✗ Error processing diagnostics event: {e}")
            # Nack and requeue on error
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

    def start_consuming(self):
        """Start consuming messages from RabbitMQ"""
        try:
            # Set QoS
            self.channel.basic_qos(prefetch_count=1)

            # Set up consumers
            self.channel.basic_consume(
                queue='backend_payment_events',
                on_message_callback=self.on_payment_event
            )
            logger.info("✓ Registered payment event consumer")

            self.channel.basic_consume(
                queue='backend_diagnostics_events',
                on_message_callback=self.on_diagnostics_event
            )
            logger.info("✓ Registered diagnostics event consumer")

            logger.info("🚀 Starting event consumer... waiting for messages")
            self.running = True
            self.channel.start_consuming()

        except Exception as e:
            logger.error(f"✗ Error in consumer: {e}")
            self.running = False
        finally:
            if self.connection and not self.connection.is_closed:
                self.connection.close()

    def stop(self):
        """Stop consuming messages"""
        if self.channel:
            self.channel.stop_consuming()
        if self.connection and not self.connection.is_closed:
            self.connection.close()
        self.running = False
        logger.info("✓ Consumer stopped")


# Singleton instance
consumer_instance = None


def get_consumer() -> RabbitMQConsumer:
    """Get or create consumer instance"""
    global consumer_instance
    if consumer_instance is None:
        consumer_instance = RabbitMQConsumer()
    return consumer_instance


def start_consumer_background():
    """Start consumer in background thread"""
    consumer = get_consumer()
    try:
        consumer.connect()
        consumer.declare_exchanges_and_queues()
    except Exception as e:
        logger.error(f"Failed to initialize consumer: {e}")
        return

    # Run in background thread
    thread = Thread(target=consumer.start_consuming, daemon=True)
    thread.start()
    logger.info("✓ Background consumer thread started")
    return thread


if __name__ == "__main__":
    # Direct execution for testing
    logging.basicConfig(level=logging.INFO)
    consumer = RabbitMQConsumer()
    consumer.connect()
    consumer.declare_exchanges_and_queues()
    consumer.start_consuming()

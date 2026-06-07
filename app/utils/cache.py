"""
Redis Caching Utilities for Backend Service (Phase 2)
Handles all Redis caching operations
"""

import redis
import json
import logging
from functools import wraps
from typing import Any, Callable, Optional, TypeVar
from datetime import timedelta

logger = logging.getLogger(__name__)

# Redis client
redis_client = redis.Redis(
    host='localhost',
    port=6379,
    db=0,
    decode_responses=True
)

T = TypeVar('T')


class CacheManager:
    """Manages all caching operations"""
    
    def __init__(self, ttl_default: int = 3600):
        """
        Initialize cache manager
        
        Args:
            ttl_default: Default TTL in seconds (1 hour)
        """
        self.ttl_default = ttl_default
        self.redis = redis_client

    def get(self, key: str) -> Optional[Any]:
        """Get value from cache"""
        try:
            value = self.redis.get(key)
            if value:
                return json.loads(value)
            return None
        except Exception as e:
            logger.error(f"Cache get error for key '{key}': {e}")
            return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """Set value in cache"""
        try:
            ttl = ttl or self.ttl_default
            self.redis.setex(key, ttl, json.dumps(value))
            logger.debug(f"Cache set: {key} (TTL: {ttl}s)")
            return True
        except Exception as e:
            logger.error(f"Cache set error for key '{key}': {e}")
            return False

    def delete(self, key: str) -> bool:
        """Delete value from cache"""
        try:
            self.redis.delete(key)
            logger.debug(f"Cache deleted: {key}")
            return True
        except Exception as e:
            logger.error(f"Cache delete error for key '{key}': {e}")
            return False

    def clear_pattern(self, pattern: str) -> int:
        """Clear all keys matching pattern"""
        try:
            keys = self.redis.keys(pattern)
            if keys:
                self.redis.delete(*keys)
                logger.debug(f"Cache cleared: {len(keys)} keys matching '{pattern}'")
                return len(keys)
            return 0
        except Exception as e:
            logger.error(f"Cache clear pattern error for '{pattern}': {e}")
            return 0

    def incr(self, key: str, amount: int = 1) -> int:
        """Increment counter in cache"""
        try:
            return self.redis.incrby(key, amount)
        except Exception as e:
            logger.error(f"Cache increment error for key '{key}': {e}")
            return 0

    def expire(self, key: str, seconds: int) -> bool:
        """Set expiration on key"""
        try:
            return self.redis.expire(key, seconds)
        except Exception as e:
            logger.error(f"Cache expire error for key '{key}': {e}")
            return False


# Global cache manager instance
cache_manager = CacheManager()


def cache_key(*args, **kwargs) -> str:
    """Generate cache key from arguments"""
    parts = [str(arg) for arg in args]
    for k, v in sorted(kwargs.items()):
        parts.append(f"{k}:{v}")
    return ":".join(parts)


def cached(ttl: Optional[int] = None) -> Callable:
    """
    Decorator to cache function results
    
    Usage:
        @cached(ttl=3600)
        def get_user(user_id: str):
            return db.query(User).get(user_id)
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            # Generate cache key
            key = cache_key(func.__name__, *args, **kwargs)
            
            # Try cache first
            cached_value = cache_manager.get(key)
            if cached_value is not None:
                logger.debug(f"Cache hit: {key}")
                return cached_value
            
            # Execute function
            result = func(*args, **kwargs)
            
            # Cache result
            if result is not None:
                cache_manager.set(key, result, ttl)
            
            return result
        return wrapper
    return decorator


# ============================================================================
# USER CACHE OPERATIONS
# ============================================================================

def cache_user(user_id: str, user_data: dict, ttl: int = 3600) -> bool:
    """Cache user data"""
    return cache_manager.set(f"user:{user_id}", user_data, ttl)


def get_cached_user(user_id: str) -> Optional[dict]:
    """Get cached user data"""
    return cache_manager.get(f"user:{user_id}")


def clear_user_cache(user_id: str) -> bool:
    """Clear all cache for user"""
    pattern = f"user:{user_id}:*"
    cache_manager.clear_pattern(pattern)
    return cache_manager.delete(f"user:{user_id}")


# ============================================================================
# SESSION CACHE OPERATIONS
# ============================================================================

def cache_session(session_id: str, session_data: dict, ttl: int = 86400) -> bool:
    """Cache session data"""
    return cache_manager.set(f"session:{session_id}", session_data, ttl)


def get_cached_session(session_id: str) -> Optional[dict]:
    """Get cached session data"""
    return cache_manager.get(f"session:{session_id}")


def invalidate_session(session_id: str) -> bool:
    """Invalidate session cache"""
    return cache_manager.delete(f"session:{session_id}")


def invalidate_user_sessions(user_id: str) -> int:
    """Invalidate all sessions for user"""
    pattern = f"session:user:{user_id}:*"
    return cache_manager.clear_pattern(pattern)


# ============================================================================
# AUTHENTICATION CACHE OPERATIONS
# ============================================================================

def cache_auth_token(token: str, user_id: str, ttl: int = 86400) -> bool:
    """Cache authentication token"""
    return cache_manager.set(f"auth_token:{token}", {"user_id": user_id}, ttl)


def get_cached_auth_token(token: str) -> Optional[dict]:
    """Get cached auth token"""
    return cache_manager.get(f"auth_token:{token}")


def invalidate_auth_token(token: str) -> bool:
    """Invalidate auth token"""
    return cache_manager.delete(f"auth_token:{token}")


def cache_failed_login(email: str) -> int:
    """Track failed login attempt"""
    key = f"failed_login:{email}"
    attempts = cache_manager.incr(key)
    if attempts == 1:
        cache_manager.expire(key, 900)  # 15 minutes expiry
    return attempts


def clear_failed_login(email: str) -> bool:
    """Clear failed login attempts"""
    return cache_manager.delete(f"failed_login:{email}")


# ============================================================================
# SUBSCRIPTION/PAYMENT CACHE OPERATIONS (From Events)
# ============================================================================

def cache_user_subscription(user_id: str, subscription_data: dict, ttl: int = 3600) -> bool:
    """Cache user subscription status"""
    return cache_manager.set(f"user:subscription:{user_id}", subscription_data, ttl)


def get_cached_subscription(user_id: str) -> Optional[dict]:
    """Get cached subscription status"""
    return cache_manager.get(f"user:subscription:{user_id}")


def cache_user_payment_status(user_id: str, status: str, ttl: int = 3600) -> bool:
    """Cache user payment status"""
    return cache_manager.set(f"user:payment_status:{user_id}", status, ttl)


def get_cached_payment_status(user_id: str) -> Optional[str]:
    """Get cached payment status"""
    return cache_manager.get(f"user:payment_status:{user_id}")


# ============================================================================
# DIAGNOSTICS CACHE OPERATIONS (From Events)
# ============================================================================

def cache_diagnostics_results(user_id: str, results: dict, ttl: int = 3600) -> bool:
    """Cache diagnostics results"""
    return cache_manager.set(f"user:diagnostics:{user_id}", results, ttl)


def get_cached_diagnostics(user_id: str) -> Optional[dict]:
    """Get cached diagnostics results"""
    return cache_manager.get(f"user:diagnostics:{user_id}")


# ============================================================================
# RATE LIMITING CACHE OPERATIONS
# ============================================================================

def check_rate_limit(user_id: str, limit: int = 100, window: int = 3600) -> bool:
    """
    Check if user is within rate limit
    
    Args:
        user_id: User ID
        limit: Max requests in window
        window: Time window in seconds
    
    Returns:
        True if within limit, False if exceeded
    """
    key = f"rate_limit:{user_id}"
    current = cache_manager.redis.get(key)
    
    if current is None:
        cache_manager.redis.setex(key, window, 1)
        return True
    
    count = int(current)
    if count >= limit:
        return False
    
    cache_manager.incr(key)
    return True


# ============================================================================
# CACHE HEALTH CHECK
# ============================================================================

def test_cache_connection() -> bool:
    """Test Redis connection"""
    try:
        redis_client.ping()
        logger.info("✓ Redis cache connection OK")
        return True
    except Exception as e:
        logger.error(f"✗ Redis cache connection failed: {e}")
        return False


def get_cache_stats() -> dict:
    """Get cache statistics"""
    try:
        info = redis_client.info()
        return {
            'connected_clients': info.get('connected_clients'),
            'used_memory': info.get('used_memory_human'),
            'total_keys': redis_client.dbsize(),
            'uptime_seconds': info.get('uptime_in_seconds')
        }
    except Exception as e:
        logger.error(f"Error getting cache stats: {e}")
        return {}


if __name__ == "__main__":
    # Test cache operations
    logging.basicConfig(level=logging.INFO)
    
    # Test connection
    if test_cache_connection():
        # Test set/get
        cache_manager.set("test:key", {"data": "test"}, 60)
        value = cache_manager.get("test:key")
        print(f"✓ Cached and retrieved: {value}")
        
        # Test expiration
        cache_manager.delete("test:key")
        print("✓ Cache operations working")

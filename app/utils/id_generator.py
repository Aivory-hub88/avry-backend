"""
ID generation utilities for AVRY-backend service.
"""

import uuid
from datetime import datetime


def generate_user_id() -> str:
    """Generate unique user ID"""
    return f"user_{uuid.uuid4().hex[:12]}"


def generate_session_id() -> str:
    """Generate unique session ID"""
    return f"session_{uuid.uuid4().hex[:16]}"


def generate_audit_id() -> str:
    """Generate unique audit log ID"""
    return f"audit_{uuid.uuid4().hex[:12]}"


def generate_token_id() -> str:
    """Generate unique token ID"""
    return f"token_{uuid.uuid4().hex[:16]}"


def generate_id(prefix: str = "") -> str:
    """Generate generic unique ID with optional prefix"""
    base_id = uuid.uuid4().hex[:16]
    if prefix:
        return f"{prefix}_{base_id}"
    return base_id


def generate_short_id() -> str:
    """Generate short unique ID (8 chars)"""
    return uuid.uuid4().hex[:8]

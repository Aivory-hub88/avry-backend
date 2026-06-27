"""
Impersonation models for Admin User Impersonation feature.
Defines request/response schemas for impersonation session lifecycle.
"""

from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime


class ImpersonationStartRequest(BaseModel):
    """Request to start an impersonation session."""
    target_user_id: str
    access_mode: Literal["read_only", "full_access"] = "read_only"


class ImpersonationStartResponse(BaseModel):
    """Response after successfully starting an impersonation session."""
    session_id: str
    target_user_id: str
    target_email: str
    access_mode: str
    expires_at: datetime
    started_at: datetime


class ImpersonationStatusResponse(BaseModel):
    """Response for current impersonation session status."""
    active: bool
    session_id: Optional[str] = None
    target_user_id: Optional[str] = None
    target_email: Optional[str] = None
    access_mode: Optional[str] = None
    expires_at: Optional[datetime] = None
    remaining_seconds: Optional[int] = None


class ImpersonationHistoryEntry(BaseModel):
    """Single entry in impersonation session history."""
    session_id: str
    admin_user_id: str
    target_user_id: str
    target_email: str
    access_mode: str
    status: str  # active, expired, terminated
    started_at: datetime
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    total_requests: int

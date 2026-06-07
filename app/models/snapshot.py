"""
Snapshot model for AVRY-backend service.
Placeholder for snapshot records.
"""

from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime


class SnapshotBase(BaseModel):
    """Base snapshot model"""
    name: str
    description: Optional[str] = None
    data: Dict[str, Any] = {}


class SnapshotCreate(SnapshotBase):
    """Create snapshot request"""
    user_id: Optional[str] = None
    diagnostic_id: Optional[str] = None
    pass


class SnapshotResponse(SnapshotBase):
    """Snapshot response"""
    snapshot_id: str
    user_id: Optional[str] = None
    diagnostic_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SnapshotRecord(BaseModel):
    """Snapshot record for database storage"""
    snapshot_id: str
    user_id: Optional[str] = None
    diagnostic_id: Optional[str] = None
    name: str
    description: Optional[str] = None
    data: Dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

"""
Diagnostic model for AVRY-backend service.
Placeholder for diagnostic records.
"""

from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime


class DiagnosticBase(BaseModel):
    """Base diagnostic model"""
    title: str
    description: Optional[str] = None
    data: Dict[str, Any] = {}


class DiagnosticCreate(DiagnosticBase):
    """Create diagnostic request"""
    user_id: Optional[str] = None
    pass


class DiagnosticResponse(DiagnosticBase):
    """Diagnostic response"""
    diagnostic_id: str
    user_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DiagnosticRecord(BaseModel):
    """Diagnostic record for database storage"""
    diagnostic_id: str
    user_id: Optional[str] = None
    title: str
    description: Optional[str] = None
    data: Dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

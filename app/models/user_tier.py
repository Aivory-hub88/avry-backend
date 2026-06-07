"""
User Tier Models for AI Operating Partner subscription management.
"""

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum


class TierLevel(str, Enum):
    """Subscription tier levels."""
    FOUNDATION = "foundation"
    ACCELERATION = "acceleration"
    INTELLIGENCE = "intelligence"


class TierLimits(BaseModel):
    """Limits for each tier."""
    max_workflows: int
    max_executions: int
    price: int


# Tier Configuration
TIER_LIMITS = {
    TierLevel.FOUNDATION: TierLimits(
        max_workflows=3,
        max_executions=3000,
        price=200
    ),
    TierLevel.ACCELERATION: TierLimits(
        max_workflows=10,
        max_executions=15000,
        price=500
    ),
    TierLevel.INTELLIGENCE: TierLimits(
        max_workflows=999999,  # Effectively unlimited
        max_executions=999999,  # Effectively unlimited
        price=1000
    )
}


class UserTierState(BaseModel):
    """User subscription state."""
    user_id: str
    tier: Optional[TierLevel] = None
    active_workflows: int = 0
    monthly_execution_count: int = 0
    execution_reset_date: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class WorkflowCreationRequest(BaseModel):
    """Request to create a new workflow."""
    user_id: str
    workflow_name: str


class WorkflowExecutionRequest(BaseModel):
    """Request to execute a workflow."""
    user_id: str
    workflow_id: str


class LimitCheckResponse(BaseModel):
    """Response for limit checks."""
    allowed: bool
    message: str
    current_count: int
    limit: int


class UpgradeRequiredResponse(BaseModel):
    """Response when upgrade is required."""
    upgrade_required: bool
    current_tier: str
    message: str
    suggested_tier: Optional[str] = None

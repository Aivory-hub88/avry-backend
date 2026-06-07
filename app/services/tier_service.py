"""
Tier Management Service for AI Operating Partner subscriptions.
Handles workflow limits, execution tracking, and upgrade logic.
"""

from datetime import datetime, timedelta
from typing import Dict, Optional
from app.models.user_tier import (
    UserTierState,
    TierLevel,
    TIER_LIMITS,
    LimitCheckResponse,
    UpgradeRequiredResponse
)


class TierService:
    """Service for managing user tiers and limits."""
    
    def __init__(self):
        # In-memory storage for demo (in production, use database)
        self.user_states: Dict[str, UserTierState] = {}
    
    def get_user_state(self, user_id: str) -> UserTierState:
        """Get or create user state."""
        if user_id not in self.user_states:
            self.user_states[user_id] = UserTierState(
                user_id=user_id,
                execution_reset_date=datetime.utcnow() + timedelta(days=30)
            )
        return self.user_states[user_id]
    
    def set_user_tier(self, user_id: str, tier: TierLevel) -> UserTierState:
        """Set user's subscription tier."""
        state = self.get_user_state(user_id)
        state.tier = tier
        state.updated_at = datetime.utcnow()
        return state
    
    def can_create_workflow(self, user_id: str) -> LimitCheckResponse:
        """Check if user can create a new workflow."""
        state = self.get_user_state(user_id)
        
        # No tier = allow for demo purposes
        if not state.tier:
            return LimitCheckResponse(
                allowed=True,
                message="No tier limit (demo mode)",
                current_count=state.active_workflows,
                limit=999
            )
        
        limits = TIER_LIMITS[state.tier]
        
        if state.active_workflows >= limits.max_workflows:
            return LimitCheckResponse(
                allowed=False,
                message=f"Workflow limit reached. You have {state.active_workflows} of {limits.max_workflows} workflows. Upgrade to create more.",
                current_count=state.active_workflows,
                limit=limits.max_workflows
            )
        
        return LimitCheckResponse(
            allowed=True,
            message="Workflow creation allowed",
            current_count=state.active_workflows,
            limit=limits.max_workflows
        )
    
    def can_execute_workflow(self, user_id: str) -> LimitCheckResponse:
        """Check if user can execute a workflow."""
        state = self.get_user_state(user_id)
        
        # Check if execution count needs reset
        if state.execution_reset_date and datetime.utcnow() >= state.execution_reset_date:
            self.reset_execution_count(user_id)
            state = self.get_user_state(user_id)
        
        # No tier = allow for demo purposes
        if not state.tier:
            return LimitCheckResponse(
                allowed=True,
                message="No tier limit (demo mode)",
                current_count=state.monthly_execution_count,
                limit=999999
            )
        
        limits = TIER_LIMITS[state.tier]
        
        if state.monthly_execution_count >= limits.max_executions:
            return LimitCheckResponse(
                allowed=False,
                message=f"Monthly execution limit reached. You have used {state.monthly_execution_count:,} of {limits.max_executions:,} executions. Upgrade to continue.",
                current_count=state.monthly_execution_count,
                limit=limits.max_executions
            )
        
        return LimitCheckResponse(
            allowed=True,
            message="Execution allowed",
            current_count=state.monthly_execution_count,
            limit=limits.max_executions
        )
    
    def increment_workflow_count(self, user_id: str) -> UserTierState:
        """Increment active workflow count."""
        state = self.get_user_state(user_id)
        state.active_workflows += 1
        state.updated_at = datetime.utcnow()
        return state
    
    def decrement_workflow_count(self, user_id: str) -> UserTierState:
        """Decrement active workflow count (when workflow is deleted)."""
        state = self.get_user_state(user_id)
        if state.active_workflows > 0:
            state.active_workflows -= 1
        state.updated_at = datetime.utcnow()
        return state
    
    def increment_execution_count(self, user_id: str) -> UserTierState:
        """Increment monthly execution count."""
        state = self.get_user_state(user_id)
        state.monthly_execution_count += 1
        state.updated_at = datetime.utcnow()
        return state
    
    def reset_execution_count(self, user_id: str) -> UserTierState:
        """Reset monthly execution count."""
        state = self.get_user_state(user_id)
        state.monthly_execution_count = 0
        state.execution_reset_date = datetime.utcnow() + timedelta(days=30)
        state.updated_at = datetime.utcnow()
        return state
    
    def get_upgrade_recommendation(self, user_id: str) -> Optional[UpgradeRequiredResponse]:
        """Get upgrade recommendation based on usage."""
        state = self.get_user_state(user_id)
        
        if not state.tier:
            return None
        
        limits = TIER_LIMITS[state.tier]
        
        # Check if user is hitting limits
        workflow_usage_percent = (state.active_workflows / limits.max_workflows) * 100
        execution_usage_percent = (state.monthly_execution_count / limits.max_executions) * 100
        
        # Recommend upgrade if usage > 80%
        if workflow_usage_percent > 80 or execution_usage_percent > 80:
            suggested_tier = None
            if state.tier == TierLevel.FOUNDATION:
                suggested_tier = TierLevel.ACCELERATION.value
            elif state.tier == TierLevel.ACCELERATION:
                suggested_tier = TierLevel.INTELLIGENCE.value
            
            return UpgradeRequiredResponse(
                upgrade_required=True,
                current_tier=state.tier.value,
                message=f"You're using {max(workflow_usage_percent, execution_usage_percent):.0f}% of your plan limits. Consider upgrading for more capacity.",
                suggested_tier=suggested_tier
            )
        
        return None


# Global service instance
tier_service = TierService()

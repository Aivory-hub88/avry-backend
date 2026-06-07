"""
Audit Logger Service - Logs all console operations for compliance
"""

import logging
from typing import Dict, Any, Optional
from datetime import datetime
import json

logger = logging.getLogger(__name__)

class AuditLogger:
    """
    Logs all console operations for audit and compliance.
    Tracks user actions, AI responses, credit usage, and errors.
    """
    
    def __init__(self):
        # In production, write to database or file system
        self.logs = []
    
    def log_console_operation(
        self,
        user_id: str,
        operation: str,
        prompt: str,
        response: str,
        credits_used: int,
        tier: str,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Log a console operation.
        
        Args:
            user_id: User identifier
            operation: Operation type
            prompt: User prompt
            response: AI response
            credits_used: Credits deducted
            tier: User tier
            metadata: Additional metadata
        """
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "user_id": user_id,
            "operation": operation,
            "prompt": prompt[:500],  # Truncate for storage
            "response": response[:500],
            "credits_used": credits_used,
            "tier": tier,
            "metadata": metadata or {}
        }
        
        self.logs.append(log_entry)
        logger.info(f"Audit log: {operation} by {user_id} (tier: {tier}, credits: {credits_used})")
    
    def log_error(
        self,
        user_id: str,
        operation: str,
        error_type: str,
        error_message: str,
        context: Optional[Dict[str, Any]] = None
    ):
        """
        Log an error.
        
        Args:
            user_id: User identifier
            operation: Operation that failed
            error_type: Error type
            error_message: Error message
            context: Additional context
        """
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "user_id": user_id,
            "operation": operation,
            "error_type": error_type,
            "error_message": error_message,
            "context": context or {}
        }
        
        self.logs.append(log_entry)
        logger.error(f"Error log: {error_type} in {operation} for {user_id}: {error_message}")
    
    def get_logs(self, user_id: Optional[str] = None, limit: int = 100) -> list:
        """
        Get audit logs.
        
        Args:
            user_id: Filter by user (optional)
            limit: Maximum number of logs to return
            
        Returns:
            List of log entries
        """
        if user_id:
            filtered = [log for log in self.logs if log.get('user_id') == user_id]
        else:
            filtered = self.logs
        
        return sorted(filtered, key=lambda x: x['timestamp'], reverse=True)[:limit]
    
    def export_logs(self, filepath: str):
        """Export logs to JSON file."""
        try:
            with open(filepath, 'w') as f:
                json.dump(self.logs, f, indent=2)
            logger.info(f"Exported {len(self.logs)} logs to {filepath}")
        except Exception as e:
            logger.error(f"Error exporting logs: {str(e)}")

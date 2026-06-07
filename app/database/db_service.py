"""
Database service for diagnostics, snapshots, and blueprints.

For MVP, uses JSON file storage. Can be replaced with PostgreSQL/MongoDB later.
"""

import json
import os
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
import logging

from app.models.diagnostic import DiagnosticRecord
from app.models.snapshot import SnapshotRecord

logger = logging.getLogger(__name__)


class DatabaseService:
    """
    File-based database service for MVP.
    
    Storage structure:
    data/
        diagnostics/
            {diagnostic_id}.json
        snapshots/
            {snapshot_id}.json
        blueprints/
            {user_id}/
                {blueprint_id}/
                    metadata.json
    """
    
    def __init__(self, base_path: str = "data"):
        """Initialize database service."""
        self.base_path = Path(base_path)
        self.diagnostics_path = self.base_path / "diagnostics"
        self.snapshots_path = self.base_path / "snapshots"
        self.blueprints_path = self.base_path / "blueprints"
        self.users_path = self.base_path / "users"
        self.sessions_path = self.base_path / "sessions"
        
        # Create directories
        self.diagnostics_path.mkdir(parents=True, exist_ok=True)
        self.snapshots_path.mkdir(parents=True, exist_ok=True)
        self.blueprints_path.mkdir(parents=True, exist_ok=True)
        self.users_path.mkdir(parents=True, exist_ok=True)
        self.sessions_path.mkdir(parents=True, exist_ok=True)
    
    # ========================================================================
    # GENERIC JSON STORAGE (for users, sessions, etc.)
    # ========================================================================
    
    def save_json(self, collection: str, item_id: str, data: dict) -> bool:
        """Save JSON data to a collection."""
        try:
            collection_path = self.base_path / collection
            collection_path.mkdir(parents=True, exist_ok=True)
            
            file_path = collection_path / f"{item_id}.json"
            with open(file_path, 'w') as f:
                json.dump(data, f, indent=2)
            
            logger.info(f"Saved {collection}/{item_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to save {collection}/{item_id}: {e}")
            return False
    
    def load_json(self, collection: str, item_id: str) -> Optional[dict]:
        """Load JSON data from a collection."""
        try:
            collection_path = self.base_path / collection
            file_path = collection_path / f"{item_id}.json"
            
            if not file_path.exists():
                return None
            
            with open(file_path, 'r') as f:
                data = json.load(f)
            
            return data
        except Exception as e:
            logger.error(f"Failed to load {collection}/{item_id}: {e}")
            return None
    
    def load_all_json(self, collection: str) -> List[dict]:
        """Load all JSON files from a collection."""
        try:
            collection_path = self.base_path / collection
            if not collection_path.exists():
                return []
            
            items = []
            for file_path in collection_path.glob("*.json"):
                with open(file_path, 'r') as f:
                    data = json.load(f)
                    items.append(data)
            
            return items
        except Exception as e:
            logger.error(f"Failed to load all from {collection}: {e}")
            return []
    
    def delete_json(self, collection: str, item_id: str) -> bool:
        """Delete JSON file from a collection."""
        try:
            collection_path = self.base_path / collection
            file_path = collection_path / f"{item_id}.json"
            
            if file_path.exists():
                file_path.unlink()
                logger.info(f"Deleted {collection}/{item_id}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to delete {collection}/{item_id}: {e}")
            return False
    
    # ========================================================================
    # DIAGNOSTICS
    # ========================================================================
    
    async def store_diagnostic(
        self,
        diagnostic_id: str,
        user_id: Optional[str],
        user_email: Optional[str],
        company_name: Optional[str],
        industry: Optional[str],
        answers: List[Dict[str, Any]],
        score: int,
        category: str
    ) -> bool:
        """Store diagnostic record."""
        try:
            record = {
                "diagnostic_id": diagnostic_id,
                "user_id": user_id,
                "user_email": user_email,
                "company_name": company_name,
                "industry": industry,
                "answers": answers,
                "score": score,
                "category": category,
                "created_at": datetime.utcnow().isoformat()
            }
            
            file_path = self.diagnostics_path / f"{diagnostic_id}.json"
            with open(file_path, 'w') as f:
                json.dump(record, f, indent=2)
            
            logger.info(f"Stored diagnostic {diagnostic_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to store diagnostic {diagnostic_id}: {e}")
            return False
    
    async def get_diagnostic(
        self,
        diagnostic_id: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve diagnostic record."""
        try:
            file_path = self.diagnostics_path / f"{diagnostic_id}.json"
            if not file_path.exists():
                logger.warning(f"Diagnostic {diagnostic_id} not found")
                return None
            
            with open(file_path, 'r') as f:
                record = json.load(f)
            
            logger.info(f"Retrieved diagnostic {diagnostic_id}")
            return record
            
        except Exception as e:
            logger.error(f"Failed to retrieve diagnostic {diagnostic_id}: {e}")
            return None
    
    async def list_diagnostics_by_user(
        self,
        user_id: str
    ) -> List[Dict[str, Any]]:
        """List all diagnostics for a user."""
        try:
            diagnostics = []
            for file_path in self.diagnostics_path.glob("*.json"):
                with open(file_path, 'r') as f:
                    record = json.load(f)
                    if record.get("user_id") == user_id:
                        diagnostics.append(record)
            
            # Sort by created_at descending
            diagnostics.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return diagnostics
            
        except Exception as e:
            logger.error(f"Failed to list diagnostics for user {user_id}: {e}")
            return []
    
    # ========================================================================
    # SNAPSHOTS
    # ========================================================================
    
    async def store_snapshot(
        self,
        snapshot_id: str,
        diagnostic_id: Optional[str],
        user_id: Optional[str],
        user_email: Optional[str],
        company_name: Optional[str],
        industry: Optional[str],
        answers: List[Dict[str, Any]],
        readiness_score: int,
        category_scores: Dict[str, float],
        primary_objective: str,
        top_recommendations: List[str],
        pain_points: List[str],
        workflows: List[str],
        key_processes: List[str],
        automation_level: str,
        data_quality_score: int
    ) -> bool:
        """Store snapshot record."""
        try:
            record = {
                "snapshot_id": snapshot_id,
                "diagnostic_id": diagnostic_id,
                "user_id": user_id,
                "user_email": user_email,
                "company_name": company_name,
                "industry": industry,
                "answers": answers,
                "readiness_score": readiness_score,
                "category_scores": category_scores,
                "primary_objective": primary_objective,
                "top_recommendations": top_recommendations,
                "pain_points": pain_points,
                "workflows": workflows,
                "key_processes": key_processes,
                "automation_level": automation_level,
                "data_quality_score": data_quality_score,
                "created_at": datetime.utcnow().isoformat()
            }
            
            file_path = self.snapshots_path / f"{snapshot_id}.json"
            with open(file_path, 'w') as f:
                json.dump(record, f, indent=2)
            
            logger.info(f"Stored snapshot {snapshot_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to store snapshot {snapshot_id}: {e}")
            return False
    
    async def get_snapshot(
        self,
        snapshot_id: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve snapshot record."""
        try:
            file_path = self.snapshots_path / f"{snapshot_id}.json"
            if not file_path.exists():
                logger.warning(f"Snapshot {snapshot_id} not found")
                return None
            
            with open(file_path, 'r') as f:
                record = json.load(f)
            
            logger.info(f"Retrieved snapshot {snapshot_id}")
            return record
            
        except Exception as e:
            logger.error(f"Failed to retrieve snapshot {snapshot_id}: {e}")
            return None
    
    async def list_snapshots_by_user(
        self,
        user_id: str
    ) -> List[Dict[str, Any]]:
        """List all snapshots for a user."""
        try:
            snapshots = []
            for file_path in self.snapshots_path.glob("*.json"):
                with open(file_path, 'r') as f:
                    record = json.load(f)
                    if record.get("user_id") == user_id:
                        snapshots.append(record)
            
            # Sort by created_at descending
            snapshots.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return snapshots
            
        except Exception as e:
            logger.error(f"Failed to list snapshots for user {user_id}: {e}")
            return []
    
    async def list_snapshots_by_diagnostic(
        self,
        diagnostic_id: str
    ) -> List[Dict[str, Any]]:
        """List all snapshots linked to a diagnostic."""
        try:
            snapshots = []
            for file_path in self.snapshots_path.glob("*.json"):
                with open(file_path, 'r') as f:
                    record = json.load(f)
                    if record.get("diagnostic_id") == diagnostic_id:
                        snapshots.append(record)
            
            # Sort by created_at descending
            snapshots.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return snapshots
            
        except Exception as e:
            logger.error(f"Failed to list snapshots for diagnostic {diagnostic_id}: {e}")
            return []
    
    # ========================================================================
    # BLUEPRINTS (update existing storage to add snapshot_id link)
    # ========================================================================
    
    async def link_blueprint_to_snapshot(
        self,
        blueprint_id: str,
        snapshot_id: str,
        user_id: str
    ) -> bool:
        """Add snapshot_id link to existing blueprint metadata."""
        try:
            # Find blueprint metadata file
            user_path = self.blueprints_path / user_id
            if not user_path.exists():
                logger.warning(f"User path {user_path} not found")
                return False
            
            # Search for blueprint
            for blueprint_dir in user_path.glob(f"{blueprint_id}"):
                metadata_file = blueprint_dir / "metadata.json"
                if metadata_file.exists():
                    with open(metadata_file, 'r') as f:
                        metadata = json.load(f)
                    
                    # Add snapshot_id
                    metadata["snapshot_id"] = snapshot_id
                    
                    with open(metadata_file, 'w') as f:
                        json.dump(metadata, f, indent=2)
                    
                    logger.info(f"Linked blueprint {blueprint_id} to snapshot {snapshot_id}")
                    return True
            
            logger.warning(f"Blueprint {blueprint_id} not found for user {user_id}")
            return False
            
        except Exception as e:
            logger.error(f"Failed to link blueprint {blueprint_id} to snapshot {snapshot_id}: {e}")
            return False


# Global database instance
db = DatabaseService()

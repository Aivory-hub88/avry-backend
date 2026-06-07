"""
avry-backend Microservice Entry Point
Description: Authentication, user management, JWT
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Create FastAPI app
app = FastAPI(
    title="AVRY Backend Service",
    description="Authentication, user management, JWT",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Import and include routes
try:
    from app.routes.auth import router as auth_router
    app.include_router(auth_router)
    print("[✓] Auth routes registered")
except Exception as e:
    print(f"[!] Warning: Could not import auth routes: {e}")

# Import database service for tier endpoint
from app.database.db_service import DatabaseService
db_service = DatabaseService(base_path="data")

# ===== TIER ENDPOINTS =====
@app.get("/api/v1/tier/state/{user_id}")
async def get_tier_state(user_id: str):
    """
    Get user's current tier and subscription state
    
    Args:
        user_id: User identifier
        
    Returns:
        User tier information
    """
    try:
        # Load user data
        user_data = db_service.load_json("users", user_id)
        if not user_data:
            return {
                "user_id": user_id,
                "tier": "unknown",
                "status": "not_found"
            }
        
        # Return tier information
        return {
            "user_id": user_id,
            "tier": user_data.get("tier", "free"),
            "account_type": user_data.get("account_type", "free"),
            "credits": user_data.get("credits", 0),
            "status": "active",
            "features": {
                "diagnostics": True,
                "blueprint": user_data.get("account_type") != "free",
                "roadmap": user_data.get("account_type") == "premium",
                "custom_reports": user_data.get("account_type") == "premium"
            }
        }
    except Exception as e:
        print(f"Error getting tier state: {e}")
        return {
            "user_id": user_id,
            "tier": "unknown",
            "status": "error",
            "error": str(e)
        }

# ===== DATABASE & MODELS ENDPOINTS =====
@app.get("/api/database/status")
async def database_status():
    """
    Database status and health check endpoint
    Returns database configuration and operational status
    """
    from datetime import datetime
    return {
        "database": "operational",
        "service": "avry-backend",
        "data_path": "data",
        "status": "ready",
        "collections": ["users", "payments", "diagnostics", "blueprints"],
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/api/models/schemas")
async def models_schemas():
    """
    Get available data models and schemas
    Returns information about database schemas used by the service
    """
    from datetime import datetime
    return {
        "models": [
            {
                "name": "User",
                "fields": ["id", "name", "email", "tier", "account_type", "credits", "created_at", "updated_at"],
                "description": "User account and profile information"
            },
            {
                "name": "Diagnostic",
                "fields": ["id", "user_id", "company_name", "industry", "score", "created_at"],
                "description": "AI readiness diagnostic assessment results"
            },
            {
                "name": "Blueprint",
                "fields": ["id", "user_id", "diagnostic_id", "status", "created_at", "updated_at"],
                "description": "Implementation blueprint generated from diagnostics"
            },
            {
                "name": "Payment",
                "fields": ["id", "user_id", "amount", "status", "timestamp", "transaction_id"],
                "description": "Payment transactions and wallet topups"
            }
        ],
        "schemas_available": 4,
        "timestamp": datetime.utcnow().isoformat()
    }

# Health check endpoint
@app.get("/health")
async def health():
    """Service health check"""
    return {
        "status": "healthy",
        "service": "avry-backend",
        "version": "1.0.0"
    }

@app.get("/")
async def root():
    """Service info"""
    return {
        "service": "AVRY Backend Service",
        "version": "1.0.0"
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8081"))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        reload=os.getenv("ENVIRONMENT", "production") == "development"
    )

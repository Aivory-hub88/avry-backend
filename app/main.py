"""
AVRY-Backend Service
Authentication, Authorization, and Tier Management
Port: 8081
"""

import os
import sys
from pathlib import Path

# Add the parent directory to the path so we can import 'app'
current_dir = Path(__file__).parent.parent
sys.path.insert(0, str(current_dir))

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from datetime import datetime

from app.config import settings
from app.database.db_service import DatabaseService
from app.llm.ollama_client import OllamaClient

# PostgreSQL pool for auth
try:
    from app.database import pg_service as pg
    _PG_MODULE = True
except ImportError:
    _PG_MODULE = False

# Register routes
try:
    from app.routes.auth import router as auth_router
    print("[✓] Auth routes registered")
except Exception as e:
    print(f"[!] Warning: Could not import auth routes: {e}")
    auth_router = None

try:
    from app.routes.impersonation import router as impersonation_router
    print("[✓] Impersonation routes registered")
except Exception as e:
    print(f"[!] Warning: Could not import impersonation routes: {e}")
    impersonation_router = None

try:
    from app.routes.logs import router as logs_router
    print("[✓] Logs routes registered")
except Exception as e:
    print(f"[!] Warning: Could not import logs routes: {e}")
    logs_router = None

try:
    from app.routes.admin_users import router as admin_users_router
    print("[✓] Admin users routes registered")
except Exception as e:
    print(f"[!] Warning: Could not import admin users routes: {e}")
    admin_users_router = None

try:
    from app.routes.telegram import router as telegram_router
    print("[✓] Telegram routes registered")
except Exception as e:
    print(f"[!] Warning: Could not import telegram routes: {e}")
    telegram_router = None

try:
    from app.routes.slack import router as slack_router
    print("[✓] Slack routes registered")
except Exception as e:
    print(f"[!] Warning: Could not import slack routes: {e}")
    slack_router = None

try:
    from app.routes.agent_actions import router as agent_actions_router
    print("[✓] Agent actions routes registered")
except Exception as e:
    print(f"[!] Warning: Could not import agent actions routes: {e}")
    agent_actions_router = None

# Import event system (Phase 2)
try:
    from app.events.consumer import start_consumer_background
    from app.events import publisher as event_publisher
    print("[✓] Event system imported")
except Exception as e:
    print(f"[!] Warning: Could not import event system: {e}")
    event_publisher = None
    start_consumer_background = None

# Initialize services
db_service = DatabaseService(base_path="data")
try:
    llm_client = OllamaClient(base_url=settings.ollama_base_url)
except Exception as e:
    print(f"[WARNING] LLM client initialization failed: {e}")
    llm_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan events - startup and shutdown"""
    print(f"[{datetime.now().isoformat()}] [STARTUP] AVRY-Backend service starting on port 8081...")

    # Init PostgreSQL pool for auth
    if _PG_MODULE:
        await pg.init_pool()

    # Start event consumer (Phase 2)
    consumer_thread = None
    if start_consumer_background:
        try:
            consumer_thread = start_consumer_background()
            print("[✓] Event consumer started in background")
        except Exception as e:
            print(f"[!] Warning: Could not start event consumer: {e}")

    yield

    print(f"[{datetime.now().isoformat()}] [SHUTDOWN] AVRY-Backend service shutting down...")

    # Close PostgreSQL pool
    if _PG_MODULE:
        await pg.close_pool()

    if consumer_thread:
        try:
            print("[*] Stopping event consumer...")
        except Exception as e:
            print(f"[!] Warning stopping consumer: {e}")

app = FastAPI(
    title="AVRY Backend Service",
    version="1.0.0",
    description="Authentication, Authorization, and Tier Management",
    lifespan=lifespan
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Impersonation middleware — detects and validates impersonation tokens on every request
try:
    from app.middleware.impersonation_middleware import ImpersonationMiddleware
    app.add_middleware(ImpersonationMiddleware)
    print("[✓] Impersonation middleware registered")
except Exception as e:
    print(f"[!] Warning: Could not register impersonation middleware: {e}")

# ===== HELPER FUNCTIONS =====
def generate_id(prefix: str = "") -> str:
    """Generate ID locally"""
    import uuid
    base_id = uuid.uuid4().hex[:16]
    if prefix:
        return f"{prefix}_{base_id}"
    return base_id

# ===== TIER ENDPOINTS (BEFORE ROUTER REGISTRATION) =====
@app.get("/api/v1/tier/state/{user_id}")
async def get_tier_state(user_id: str):
    """
    Get user's current tier and subscription state
    
    Args:
        user_id: User identifier
        
    Returns:
        User tier information
    """
    print(f"[DEBUG] Tier endpoint called for user: {user_id}")
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

# Register routes (AFTER tier endpoints)
if auth_router:
    app.include_router(auth_router)

if impersonation_router:
    app.include_router(impersonation_router)

if logs_router:
    app.include_router(logs_router)

if admin_users_router:
    app.include_router(admin_users_router)

if telegram_router:
    app.include_router(telegram_router)
if slack_router:
    app.include_router(slack_router)
if agent_actions_router:
    app.include_router(agent_actions_router)

# ===== HEALTH CHECK =====
@app.get("/health")
async def health():
    """Health check endpoint"""
    pg_status = "connected" if (_PG_MODULE and await pg.is_available()) else "file-only"
    return {
        "status": "healthy",
        "service": "avry-backend",
        "version": "1.0.0",
        "port": 8081,
        "timestamp": datetime.utcnow().isoformat(),
        "database": pg_status,
        "llm": "available" if llm_client else "unavailable"
    }

# ===== DATABASE STATUS =====
@app.get("/api/database/status")
async def get_database_status():
    """Check database connection status"""
    try:
        users = db_service.load_all_json("users")
        return {
            "status": "connected",
            "database_type": "file-based (JSON)",
            "data_path": "data/",
            "users_count": len(users) if users else 0,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }

# ===== MODELS SCHEMA ENDPOINT =====
@app.get("/api/models/schemas")
async def get_models_schemas():
    """List available data models"""
    return {
        "models": [
            "user",
            "diagnostic",
            "snapshot",
            "user_tier",
            "wallet",
            "blueprint"
        ],
        "database_type": "file-based",
        "collections": ["users", "diagnostics", "snapshots", "tiers", "wallets"]
    }

# ===== STARTUP DEBUG =====
@app.get("/api/debug/info")
async def debug_info():
    """Debug information"""
    return {
        "service": "avry-backend",
        "port": 8081,
        "version": "1.0.0",
        "python_version": sys.version,
        "fastapi_version": "0.104.1",
        "database": {
            "type": "file-based (JSON)",
            "path": "data/",
            "collections": ["users", "sessions", "tiers", "limits"]
        },
        "timestamp": datetime.utcnow().isoformat()
    }

# ===== DEBUG CACHE ENDPOINTS (Phase 2) =====
@app.get("/api/debug/cache/subscription/{user_id}")
async def debug_subscription(user_id: str):
    """Debug endpoint to check if subscription events received"""
    try:
        from app.utils.cache import get_cached_subscription
        sub = get_cached_subscription(user_id)
        return {
            "user_id": user_id,
            "subscription": sub,
            "source": "rabbitmq_event_cache",
            "status": "✓ Events flowing" if sub else "ℹ Waiting for events"
        }
    except Exception as e:
        return {
            "user_id": user_id,
            "subscription": None,
            "error": str(e),
            "status": "Cache system not available"
        }

@app.get("/api/debug/cache/diagnostics/{user_id}")
async def debug_diagnostics(user_id: str):
    """Debug endpoint to check if diagnostics events received"""
    try:
        from app.utils.cache import get_cached_diagnostics
        diag = get_cached_diagnostics(user_id)
        return {
            "user_id": user_id,
            "diagnostics": diag,
            "source": "rabbitmq_event_cache",
            "status": "✓ Events flowing" if diag else "ℹ Waiting for events"
        }
    except Exception as e:
        return {
            "user_id": user_id,
            "diagnostics": None,
            "error": str(e),
            "status": "Cache system not available"
        }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8081))
    debug = os.getenv("DEBUG", "False").lower() == "true"
    print(f"\n[*] Starting AVRY-Backend on port {port}...")
    print(f"[*] Debug mode: {debug}")
    print(f"[*] Open browser to: http://localhost:{port}/docs\n")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        reload=False,  # Disabled to avoid reload issues
        log_level="info"
    )

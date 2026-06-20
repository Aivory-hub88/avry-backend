"""
avry-backend Microservice Entry Point
Authentication, user management, JWT — PostgreSQL-backed
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

# PostgreSQL pool for auth
try:
    from app.database import pg_service as pg
    _PG = True
except ImportError:
    _PG = False

from app.database.db_service import DatabaseService
db_service = DatabaseService(base_path="data")


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[STARTUP] avry-backend starting...")
    if _PG:
        await pg.init_pool()
    yield
    print("[SHUTDOWN] avry-backend stopping...")
    if _PG:
        await pg.close_pool()


app = FastAPI(
    title="AVRY Backend Service",
    description="Authentication, user management, JWT",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    from app.routes.auth import router as auth_router
    app.include_router(auth_router)
    print("[OK] Auth routes registered")
except Exception as e:
    print(f"[!] Auth routes failed: {e}")

try:
    from app.routes.templates import router as templates_router
    app.include_router(templates_router)
    from app.routes.agents import router as agents_router
    app.include_router(agents_router)
    print("[OK] Templates & Agents routes registered")
except Exception as e:
    print(f"[!] Templates/Agents routes failed: {e}")

try:
    from app.routes.agent_catalog import router as agent_catalog_router
    app.include_router(agent_catalog_router)
    print("[OK] Agent catalog routes registered")
except Exception as e:
    print(f"[!] Agent catalog routes failed: {e}")


@app.get("/health")
async def health():
    pg_ok = _PG and await pg.is_available()
    return {
        "status": "healthy",
        "service": "avry-backend",
        "version": "1.0.0",
        "database": "postgresql" if pg_ok else "file-only",
    }


@app.get("/api/v1/tier/state/{user_id}")
async def get_tier_state(user_id: str):
    try:
        user_data = db_service.load_json("users", user_id)
        if not user_data:
            return {"user_id": user_id, "tier": "unknown", "status": "not_found"}
        return {
            "user_id": user_id,
            "tier": user_data.get("tier", "free"),
            "account_type": user_data.get("account_type", "free"),
            "credits": user_data.get("credits", 0),
            "status": "active",
        }
    except Exception as e:
        return {"user_id": user_id, "tier": "unknown", "status": "error", "error": str(e)}


@app.get("/api/database/status")
async def database_status():
    pg_ok = _PG and await pg.is_available()
    return {
        "database": "postgresql" if pg_ok else "file-based",
        "service": "avry-backend",
        "status": "ready",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/")
async def root():
    return {"service": "AVRY Backend Service", "version": "1.0.0"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8081"))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False, log_level="info")

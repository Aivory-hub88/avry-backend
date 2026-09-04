"""
avry-backend Microservice Entry Point
Authentication, user management, JWT — PostgreSQL-backed
"""
import asyncio
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

    # Account-cleanup poller (see app/services/account_cleanup.py). Runs
    # regardless of ACCOUNT_CLEANUP_ENABLED — disabled just means it logs
    # candidates instead of acting on them.
    cleanup_task = None
    if _PG:
        try:
            from app.services import account_cleanup
            cleanup_task = asyncio.create_task(account_cleanup.run_poller())
            print("[OK] Account-cleanup poller started")
        except Exception as e:
            print(f"[!] Account-cleanup poller failed to start: {e}")

    yield
    print("[SHUTDOWN] avry-backend stopping...")
    if cleanup_task:
        cleanup_task.cancel()
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

try:
    from app.routes.impersonation import router as impersonation_router
    app.include_router(impersonation_router)
    print("[OK] Impersonation routes registered")
except Exception as e:
    print(f"[!] Impersonation routes failed: {e}")

try:
    from app.routes.logs import router as logs_router
    app.include_router(logs_router)
    print("[OK] Logs routes registered")
except Exception as e:
    print(f"[!] Logs routes failed: {e}")

try:
    from app.routes.agent_actions import router as agent_actions_router
    app.include_router(agent_actions_router)
    print("[OK] Agent actions routes registered")
except Exception as e:
    print(f"[!] Agent actions routes failed: {e}")

try:
    from app.routes.agent_profiles import router as agent_profiles_router
    app.include_router(agent_profiles_router)
    print("[OK] Agent profiles routes registered")
except Exception as e:
    print(f"[!] Agent profiles routes failed: {e}")

try:
    from app.routes.agent_tool_scope import router as agent_tool_scope_router
    app.include_router(agent_tool_scope_router)
    print("[OK] Agent tool scope routes registered")
except Exception as e:
    print(f"[!] Agent tool scope routes failed: {e}")

try:
    from app.routes.agent_approvals import router as agent_approvals_router
    app.include_router(agent_approvals_router)
    print("[OK] Agent approvals routes registered")
except Exception as e:
    print(f"[!] Agent approvals routes failed: {e}")

try:
    from app.routes.agent_memory import router as agent_memory_router
    app.include_router(agent_memory_router)
    print("[OK] Agent memory routes registered")
except Exception as e:
    print(f"[!] Agent memory routes failed: {e}")

try:
    from app.routes.agent_api_keys import router as agent_api_keys_router
    app.include_router(agent_api_keys_router)
    print("[OK] Agent API key routes registered")
except Exception as e:
    print(f"[!] Agent API key routes failed: {e}")

try:
    from app.routes.tenant_mcp_servers import router as tenant_mcp_servers_router
    app.include_router(tenant_mcp_servers_router)
    print("[OK] Tenant custom MCP server routes registered")
except Exception as e:
    print(f"[!] Tenant custom MCP server routes failed: {e}")

try:
    from app.routes.tenant_scheduled_runs import router as tenant_scheduled_runs_router
    app.include_router(tenant_scheduled_runs_router)
    print("[OK] Tenant scheduled run routes registered")
except Exception as e:
    print(f"[!] Tenant scheduled run routes failed: {e}")

try:
    from app.routes.telegram import router as telegram_router
    app.include_router(telegram_router)
    print("[OK] Telegram routes registered")
except Exception as e:
    print(f"[!] Telegram routes failed: {e}")

try:
    from app.routes.discord import router as discord_router
    app.include_router(discord_router)
    print("[OK] Discord routes registered")
except Exception as e:
    print(f"[!] Discord routes failed: {e}")

try:
    from app.routes.slack import router as slack_router
    app.include_router(slack_router)
    print("[OK] Slack routes registered")
except Exception as e:
    print(f"[!] Slack routes failed: {e}")

try:
    from app.routes.credits import router as credits_router
    app.include_router(credits_router)
    print("[OK] Credits routes registered")
except Exception as e:
    print(f"[!] Credits routes failed: {e}")

# The container runs THIS file (`python main.py`), not app/main.py, and only
# app/main.py had ever registered this router — so every free-assessment lead
# and funnel event POSTed by the landing site hit an unmounted path and came
# back 404. Nothing was stored and nothing was logged as an error, because a
# 404 is not an exception; the misses are only visible in the access log.
try:
    from app.routes.assessment_leads import router as assessment_leads_router
    app.include_router(assessment_leads_router)
    print("[OK] Assessment leads routes registered")
except Exception as e:
    print(f"[!] Assessment leads routes failed: {e}")

try:
    from app.routes.trap_hits import router as trap_hits_router
    app.include_router(trap_hits_router)
    print("[OK] Trap hits routes registered")
except Exception as e:
    print(f"[!] Trap hits routes failed: {e}")

try:
    from app.routes.admin_users import router as admin_users_router
    app.include_router(admin_users_router)
    print("[OK] Admin users routes registered")
except Exception as e:
    print(f"[!] Warning: Could not import admin users routes: {e}")

# Reads/writes identity.user_tiers — avry-payments calls
# POST /api/v1/entitlements/internal/grant after every settled purchase, but
# this router was never registered here, so no real purchase has ever
# actually landed an entitlement. Required for Policy B (subscription lapse,
# app/services/account_cleanup.py) to ever have real data to act on.
try:
    from app.routes.entitlements import router as entitlements_router
    app.include_router(entitlements_router)
    print("[OK] Entitlements routes registered")
except Exception as e:
    print(f"[!] Entitlements routes failed: {e}")

# Impersonation middleware
try:
    from app.middleware.impersonation_middleware import ImpersonationMiddleware
    app.add_middleware(ImpersonationMiddleware)
    print("[OK] Impersonation middleware registered")
except Exception as e:
    print(f"[!] Impersonation middleware failed: {e}")


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

"""AVRY-Backend Service - Fresh clean version"""
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime

app = FastAPI(
    title="AVRY Backend Service",
    version="1.0.0",
    description="Authentication and Authorization Service"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === SIMPLE ROUTES ===
@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "avry-backend",
        "port": 8081,
        "timestamp": datetime.utcnow().isoformat()
    }

@app.post("/api/v1/auth/login")
async def login(email: str = None, password: str = None):
    """Login endpoint"""
    return {
        "success": True,
        "access_token": "test_token_123",
        "token_type": "bearer",
        "user": {"email": email, "id": "user_123"}
    }

@app.post("/api/v1/auth/register")
async def register(email: str = None, password: str = None, company_name: str = None):
    """Register endpoint"""
    return {
        "success": True,
        "user_id": "new_user_123",
        "email": email,
        "tier": "free"
    }

@app.get("/api/v1/auth/me")
async def get_me():
    """Get current user"""
    return {"id": "user_123", "email": "test@test.com", "tier": "free"}

@app.get("/api/v1/tier/state/{user_id}")
async def get_tier(user_id: str):
    """Get user tier"""
    return {"user_id": user_id, "tier": "free", "credits": 100}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8081))
    print(f"\n[*] Starting AVRY-Backend on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)

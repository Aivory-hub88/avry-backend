"""
Authentication service for user management and JWT tokens.
"""

import os
import bcrypt
import jwt
from datetime import datetime, timedelta
from typing import Optional, Tuple
import secrets

from app.models.user import (
    User, UserCreate, UserLogin, UserResponse, 
    Session, TokenPair, AuthResponse
)
from app.utils.id_generator import generate_id


# JWT Configuration
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 7


class AuthService:
    """Handles authentication, user management, and JWT tokens"""
    
    def __init__(self, db_service):
        self.db = db_service
        self._ensure_super_admin()
    
    def _ensure_super_admin(self):
        """Create super admin account if it doesn't exist"""
        try:
            # Check if super admin exists
            users = self.db.load_all_json("users")
            super_admin_exists = any(
                u.get("email") == "grandmaster@aivory.ai" 
                for u in users
            )
            
            if not super_admin_exists:
                # Get password from environment
                password = os.getenv("SUPERADMIN_PASSWORD")
                if not password:
                    raise ValueError("SUPERADMIN_PASSWORD environment variable is required")
                
                # Create super admin account
                user_id = "GrandMasterRCH"
                password_hash = bcrypt.hashpw(
                    password.encode('utf-8'), 
                    bcrypt.gensalt(12)
                ).decode('utf-8')
                
                super_admin = {
                    "user_id": user_id,
                    "email": "grandmaster@aivory.ai",
                    "password_hash": password_hash,
                    "account_type": "superadmin",
                    "company_name": "Aivory",
                    "created_at": datetime.utcnow().isoformat(),
                    "updated_at": datetime.utcnow().isoformat()
                }
                
                self.db.save_json("users", user_id, super_admin)
                print(f"✓ Super admin account created: grandmaster@aivory.ai")
                
                # Seed super admin payment records - all products pre-purchased
                self._seed_super_admin_payments(user_id)
        except Exception as e:
            print(f"Warning: Could not create super admin: {e}")
    
    def _seed_super_admin_payments(self, user_id: str):
        """Seed payment records for super admin - all products pre-purchased"""
        try:
            superadmin_payments = [
                {
                    "payment_id": "superadmin_snapshot_lifetime",
                    "user_id": user_id,
                    "product": "ai_snapshot",
                    "amount": 29.00,
                    "status": "paid",
                    "payment_method": "seeded",
                    "created_at": "2024-01-01T00:00:00Z"
                },
                {
                    "payment_id": "superadmin_blueprint_lifetime",
                    "user_id": user_id,
                    "product": "ai_blueprint",
                    "amount": 85.00,
                    "status": "paid",
                    "payment_method": "seeded",
                    "created_at": "2024-01-01T00:00:00Z"
                },
                {
                    "payment_id": "superadmin_subscription_lifetime",
                    "user_id": user_id,
                    "product": "step3_subscription",
                    "amount": 0.00,
                    "status": "paid",
                    "payment_method": "seeded",
                    "created_at": "2024-01-01T00:00:00Z"
                }
            ]
            
            for payment in superadmin_payments:
                self.db.save_json("payments", payment["payment_id"], payment)
            
            print(f"✓ Super admin payment records seeded: Snapshot, Blueprint, Subscription")
            
            # Seed diagnostic, snapshot, and blueprint data
            self._seed_super_admin_data(user_id)
        except Exception as e:
            print(f"Warning: Could not seed super admin payments: {e}")
    
    def _seed_super_admin_data(self, user_id: str):
        """Seed diagnostic, snapshot, and blueprint data for super admin"""
        try:
            # Seed diagnostic
            diagnostic_id = "superadmin_diagnostic_001"
            diagnostic = {
                "diagnostic_id": diagnostic_id,
                "user_id": user_id,
                "user_email": "grandmaster@aivory.ai",
                "company_name": "Aivory",
                "industry": "AI/ML SaaS",
                "answers": [
                    {"question": "What is your primary business objective?", "answer": "Scale AI automation platform"},
                    {"question": "Current automation level?", "answer": "Advanced"}
                ],
                "score": 95,
                "category": "AI-Ready",
                "created_at": "2024-01-01T00:00:00Z"
            }
            self.db.save_json("diagnostics", diagnostic_id, diagnostic)
            
            # Seed snapshot
            snapshot_id = "superadmin_snapshot_001"
            snapshot = {
                "snapshot_id": snapshot_id,
                "diagnostic_id": diagnostic_id,
                "user_id": user_id,
                "user_email": "grandmaster@aivory.ai",
                "company_name": "Aivory",
                "industry": "AI/ML SaaS",
                "answers": [],
                "readiness_score": 95,
                "category_scores": {
                    "data_quality": 95,
                    "process_maturity": 90,
                    "technical_readiness": 98
                },
                "primary_objective": "Scale AI automation platform",
                "top_recommendations": [
                    "Implement multi-agent orchestration",
                    "Deploy real-time monitoring",
                    "Expand integration ecosystem"
                ],
                "pain_points": ["Manual workflow management", "Limited scalability"],
                "workflows": ["Customer onboarding", "Support automation", "Data processing"],
                "key_processes": ["Lead qualification", "Ticket routing", "Report generation"],
                "automation_level": "Advanced",
                "data_quality_score": 95,
                "created_at": "2024-01-01T00:00:00Z"
            }
            self.db.save_json("snapshots", snapshot_id, snapshot)
            
            # Seed blueprint using blueprint storage service
            # Import here to avoid circular dependency
            from app.services.blueprint_storage import BlueprintStorageService
            import asyncio
            
            blueprint_storage = BlueprintStorageService()
            blueprint_id = "superadmin_blueprint_001"
            
            blueprint_json = {
                "schema_version": "aivory-v1",
                "blueprint_id": blueprint_id,
                "snapshot_id": snapshot_id,
                "user_id": user_id,
                "system_name": "Aivory Enterprise AI Platform",
                "deployment_estimate": "2-3 weeks",
                "agents": [
                    {
                        "name": "Lead Qualification Agent",
                        "trigger": "New lead submission",
                        "tools": ["CRM API", "Email", "Scoring Engine"]
                    },
                    {
                        "name": "Support Routing Agent",
                        "trigger": "Support ticket created",
                        "tools": ["Ticket System", "NLP Classifier", "Slack"]
                    },
                    {
                        "name": "Report Generation Agent",
                        "trigger": "Scheduled daily",
                        "tools": ["Database", "PDF Generator", "Email"]
                    }
                ],
                "workflows": [
                    {
                        "name": "Automated Lead Processing",
                        "description": "Qualify, score, and route leads automatically",
                        "agents": ["Lead Qualification Agent"]
                    },
                    {
                        "name": "Intelligent Support Triage",
                        "description": "Classify and route support tickets to appropriate teams",
                        "agents": ["Support Routing Agent"]
                    },
                    {
                        "name": "Daily Analytics Reports",
                        "description": "Generate and distribute daily performance reports",
                        "agents": ["Report Generation Agent"]
                    }
                ],
                "integrations_required": [
                    {
                        "service_name": "Salesforce",
                        "integration_type": "API",
                        "reason": "CRM data access",
                        "priority": "high"
                    },
                    {
                        "service_name": "Zendesk",
                        "integration_type": "Webhook",
                        "reason": "Support ticket management",
                        "priority": "high"
                    },
                    {
                        "service_name": "Slack",
                        "integration_type": "Bot",
                        "reason": "Team notifications",
                        "priority": "medium"
                    }
                ],
                "created_at": "2024-01-01T00:00:00Z"
            }
            
            # Create minimal PDF placeholder (actual PDF generation would be done by pdf_renderer)
            pdf_placeholder = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n3 0 obj\n<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /MediaBox [0 0 612 792] /Contents 4 0 R >>\nendobj\n4 0 obj\n<< /Length 44 >>\nstream\nBT\n/F1 12 Tf\n100 700 Td\n(Aivory Blueprint) Tj\nET\nendstream\nendobj\nxref\n0 5\n0000000000 65535 f\n0000000009 00000 n\n0000000058 00000 n\n0000000115 00000 n\n0000000317 00000 n\ntrailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n410\n%%EOF"
            
            # Store blueprint - handle event loop properly
            try:
                # Try to get existing event loop
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If loop is running, create task instead
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(
                            asyncio.run,
                            blueprint_storage.store_blueprint(
                                user_id=user_id,
                                blueprint_id=blueprint_id,
                                version="1.0",
                                json_data=blueprint_json,
                                pdf_bytes=pdf_placeholder
                            )
                        )
                        result = future.result(timeout=10)
                else:
                    # If no loop running, use run_until_complete
                    result = loop.run_until_complete(
                        blueprint_storage.store_blueprint(
                            user_id=user_id,
                            blueprint_id=blueprint_id,
                            version="1.0",
                            json_data=blueprint_json,
                            pdf_bytes=pdf_placeholder
                        )
                    )
            except RuntimeError:
                # Fallback: create new loop in thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run,
                        blueprint_storage.store_blueprint(
                            user_id=user_id,
                            blueprint_id=blueprint_id,
                            version="1.0",
                            json_data=blueprint_json,
                            pdf_bytes=pdf_placeholder
                        )
                    )
                    result = future.result(timeout=10)
            
            if result.success:
                print(f"✓ Super admin data seeded: diagnostic, snapshot, blueprint")
                print(f"  Blueprint ID: {blueprint_id}")
            else:
                print(f"Warning: Blueprint storage failed: {result.message}")
            
        except Exception as e:
            print(f"Warning: Could not seed super admin data: {e}")
            import traceback
            traceback.print_exc()
    
    def hash_password(self, password: str) -> str:
        """Hash password using bcrypt"""
        return bcrypt.hashpw(
            password.encode('utf-8'), 
            bcrypt.gensalt(12)
        ).decode('utf-8')
    
    def verify_password(self, password: str, password_hash: str) -> bool:
        """Verify password against hash"""
        return bcrypt.checkpw(
            password.encode('utf-8'), 
            password_hash.encode('utf-8')
        )
    
    def create_access_token(self, user: dict) -> str:
        """Create JWT access token (15 minutes)"""
        payload = {
            "user_id": user["user_id"],
            "email": user["email"],
            "account_type": user["account_type"],
            "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
            "iat": datetime.utcnow()
        }
        return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    
    def create_refresh_token(self, user_id: str, session_id: str) -> str:
        """Create JWT refresh token (7 days)"""
        payload = {
            "user_id": user_id,
            "session_id": session_id,
            "exp": datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
            "iat": datetime.utcnow()
        }
        return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    
    def verify_token(self, token: str) -> Optional[dict]:
        """Verify and decode JWT token"""
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            return payload
        except jwt.ExpiredSignatureError:
            return None
        except jwt.InvalidTokenError:
            return None
    
    async def register(self, user_data: UserCreate) -> AuthResponse:
        """Register new user"""
        # Check if email already exists
        users = self.db.load_all_json("users")
        if any(u.get("email") == user_data.email for u in users):
            raise ValueError("Email already registered")
        
        # Create user
        user_id = generate_id("user")
        password_hash = self.hash_password(user_data.password)
        
        user = {
            "user_id": user_id,
            "email": user_data.email,
            "password_hash": password_hash,
            "account_type": "free",
            "company_name": user_data.company_name,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }
        
        # Save user
        self.db.save_json("users", user_id, user)
        
        # Create session and tokens
        session_id = generate_id("session")
        refresh_token = self.create_refresh_token(user_id, session_id)
        access_token = self.create_access_token(user)
        
        # Save session
        session = {
            "session_id": session_id,
            "user_id": user_id,
            "refresh_token": refresh_token,
            "expires_at": (datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)).isoformat(),
            "created_at": datetime.utcnow().isoformat()
        }
        self.db.save_json("sessions", session_id, session)
        
        # Check for paid products
        payments = self.db.load_all_json("payments")
        user_payments = [p for p in payments if p.get("user_id") == user_id and p.get("status") == "paid"]
        
        has_bundle_payment = any(p.get("product") == "ai_bundle" for p in user_payments)
        has_snapshot_payment = any(p.get("product") == "ai_snapshot" for p in user_payments) or has_bundle_payment
        has_blueprint_payment = any(p.get("product") == "ai_blueprint" for p in user_payments) or has_bundle_payment
        has_subscription_payment = any(p.get("product") == "step3_subscription" for p in user_payments)
        
        # Check for completed diagnostics/snapshots/blueprints
        diagnostics = self.db.load_all_json("diagnostics")
        snapshots = self.db.load_all_json("snapshots")
        blueprints = self.db.load_all_json("blueprints")
        
        has_diagnostic = any(d.get("user_id") == user_id for d in diagnostics)
        has_snapshot = any(s.get("user_id") == user_id for s in snapshots)
        has_blueprint = any(b.get("user_id") == user_id for b in blueprints)
        
        # Determine tier
        if has_subscription_payment:
            tier = "enterprise"
            is_subscribed = True
            credits = 2000
            credits_max = 2000
        elif has_blueprint_payment:
            tier = "blueprint"
            is_subscribed = False
            credits = 100
            credits_max = 100
        elif has_snapshot_payment:
            tier = "snapshot"
            is_subscribed = False
            credits = 50
            credits_max = 50
        else:
            tier = "free"
            is_subscribed = False
            credits = 10
            credits_max = 10
        
        user_response = UserResponse(
            user_id=user["user_id"],
            email=user["email"],
            account_type=user["account_type"],
            company_name=user.get("company_name"),
            created_at=datetime.fromisoformat(user["created_at"]),
            tier=tier,
            is_subscribed=is_subscribed,
            has_diagnostic=has_diagnostic,
            has_snapshot=has_snapshot,
            has_blueprint=has_blueprint,
            credits=credits,
            credits_max=credits_max
        )
        
        tokens = TokenPair(
            access_token=access_token,
            refresh_token=refresh_token
        )
        
        return AuthResponse(user=user_response, tokens=tokens)
    
    async def login(self, credentials: UserLogin) -> AuthResponse:
        """Login user"""
        # Find user by email
        users = self.db.load_all_json("users")
        user = next((u for u in users if u.get("email") == credentials.email), None)
        
        if not user:
            raise ValueError("Invalid email or password")
        
        # Verify password
        if not self.verify_password(credentials.password, user["password_hash"]):
            raise ValueError("Invalid email or password")
        
        # Create session and tokens
        session_id = generate_id("session")
        refresh_token = self.create_refresh_token(user["user_id"], session_id)
        access_token = self.create_access_token(user)
        
        # Save session
        session = {
            "session_id": session_id,
            "user_id": user["user_id"],
            "refresh_token": refresh_token,
            "expires_at": (datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)).isoformat(),
            "created_at": datetime.utcnow().isoformat()
        }
        self.db.save_json("sessions", session_id, session)
        
        # Return enriched response with completion flags
        user_id = user["user_id"]
        
        # Check for paid products
        payments = self.db.load_all_json("payments")
        user_payments = [p for p in payments if p.get("user_id") == user_id and p.get("status") == "paid"]
        
        has_bundle_payment = any(p.get("product") == "ai_bundle" for p in user_payments)
        has_snapshot_payment = any(p.get("product") == "ai_snapshot" for p in user_payments) or has_bundle_payment
        has_blueprint_payment = any(p.get("product") == "ai_blueprint" for p in user_payments) or has_bundle_payment
        has_subscription_payment = any(p.get("product") == "step3_subscription" for p in user_payments)
        
        # Check for completed diagnostics/snapshots/blueprints
        diagnostics = self.db.load_all_json("diagnostics")
        snapshots = self.db.load_all_json("snapshots")
        blueprints = self.db.load_all_json("blueprints")
        
        has_diagnostic = any(d.get("user_id") == user_id for d in diagnostics)
        has_snapshot = any(s.get("user_id") == user_id for s in snapshots)
        has_blueprint = any(b.get("user_id") == user_id for b in blueprints)
        
        # Determine tier
        account_type = user.get("account_type", "free")
        is_superadmin = account_type == "superadmin"
        
        if is_superadmin:
            tier = "enterprise"
            is_subscribed = True
            # Superadmin has everything
            has_diagnostic = True
            has_snapshot = True
            has_blueprint = True
            credits = 2000
            credits_max = 2000
        elif has_subscription_payment:
            tier = "enterprise"
            is_subscribed = True
            credits = 2000
            credits_max = 2000
        elif has_blueprint_payment:
            tier = "blueprint"
            is_subscribed = False
            credits = 100
            credits_max = 100
        elif has_snapshot_payment:
            tier = "snapshot"
            is_subscribed = False
            credits = 50
            credits_max = 50
        else:
            tier = "free"
            is_subscribed = False
            credits = 10
            credits_max = 10
        
        user_response = UserResponse(
            user_id=user["user_id"],
            email=user["email"],
            account_type=user["account_type"],
            company_name=user.get("company_name"),
            created_at=datetime.fromisoformat(user["created_at"]),
            tier=tier,
            is_subscribed=is_subscribed,
            has_diagnostic=has_diagnostic,
            has_snapshot=has_snapshot,
            has_blueprint=has_blueprint,
            credits=credits,
            credits_max=credits_max
        )
        
        tokens = TokenPair(
            access_token=access_token,
            refresh_token=refresh_token
        )
        
        return AuthResponse(user=user_response, tokens=tokens)
    
    async def refresh_access_token(self, refresh_token: str) -> TokenPair:
        """Refresh access token using refresh token"""
        # Verify refresh token
        payload = self.verify_token(refresh_token)
        if not payload:
            raise ValueError("Invalid or expired refresh token")
        
        # Check session exists
        session_id = payload.get("session_id")
        session = self.db.load_json("sessions", session_id)
        if not session:
            raise ValueError("Session not found")
        
        # Check session not expired
        expires_at = datetime.fromisoformat(session["expires_at"])
        if datetime.utcnow() > expires_at:
            raise ValueError("Session expired")
        
        # Get user
        user_id = payload.get("user_id")
        user = self.db.load_json("users", user_id)
        if not user:
            raise ValueError("User not found")
        
        # Create new access token
        access_token = self.create_access_token(user)
        
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token
        )
    
    async def logout(self, refresh_token: str) -> bool:
        """Logout user by invalidating session"""
        # Verify refresh token
        payload = self.verify_token(refresh_token)
        if not payload:
            return False
        
        # Delete session
        session_id = payload.get("session_id")
        try:
            self.db.delete_json("sessions", session_id)
            return True
        except:
            return False
    
    async def get_current_user(self, access_token: str) -> Optional[UserResponse]:
        """Get current user from access token with completion flags"""
        payload = self.verify_token(access_token)
        if not payload:
            return None
        
        user_id = payload.get("user_id")
        user = self.db.load_json("users", user_id)
        if not user:
            return None
        
        # Determine tier and subscription status
        account_type = user.get("account_type", "free")
        is_superadmin = account_type == "superadmin"
        
        # Check for paid products
        payments = self.db.load_all_json("payments")
        user_payments = [p for p in payments if p.get("user_id") == user_id and p.get("status") == "paid"]
        
        has_bundle_payment = any(p.get("product") == "ai_bundle" for p in user_payments)
        has_snapshot_payment = any(p.get("product") == "ai_snapshot" for p in user_payments) or has_bundle_payment
        has_blueprint_payment = any(p.get("product") == "ai_blueprint" for p in user_payments) or has_bundle_payment
        has_subscription_payment = any(p.get("product") == "step3_subscription" for p in user_payments)
        
        # Check for completed diagnostics/snapshots/blueprints
        diagnostics = self.db.load_all_json("diagnostics")
        snapshots = self.db.load_all_json("snapshots")
        blueprints = self.db.load_all_json("blueprints")
        
        has_diagnostic = any(d.get("user_id") == user_id for d in diagnostics)
        has_snapshot = any(s.get("user_id") == user_id for s in snapshots)
        has_blueprint = any(b.get("user_id") == user_id for b in blueprints)
        
        # Determine tier
        if is_superadmin:
            tier = "enterprise"
            is_subscribed = True
            # Superadmin has everything
            has_diagnostic = True
            has_snapshot = True
            has_blueprint = True
            credits = 2000
            credits_max = 2000
        elif has_subscription_payment:
            tier = "enterprise"  # or operator based on subscription level
            is_subscribed = True
            credits = 2000
            credits_max = 2000
        elif has_blueprint_payment:
            tier = "blueprint"
            is_subscribed = False
            credits = 100
            credits_max = 100
        elif has_snapshot_payment:
            tier = "snapshot"
            is_subscribed = False
            credits = 50
            credits_max = 50
        else:
            tier = "free"
            is_subscribed = False
            credits = 10
            credits_max = 10
        
        return UserResponse(
            user_id=user["user_id"],
            email=user["email"],
            account_type=user["account_type"],
            company_name=user.get("company_name"),
            created_at=datetime.fromisoformat(user["created_at"]),
            tier=tier,
            is_subscribed=is_subscribed,
            has_diagnostic=has_diagnostic,
            has_snapshot=has_snapshot,
            has_blueprint=has_blueprint,
            credits=credits,
            credits_max=credits_max
        )
    
    async def migrate_ids_to_user(
        self, 
        user_id: str, 
        diagnostic_id: Optional[str] = None,
        snapshot_id: Optional[str] = None,
        blueprint_id: Optional[str] = None
    ) -> dict:
        """Migrate localStorage IDs to user account"""
        migrated = {
            "diagnostic": False,
            "snapshot": False,
            "blueprint": False
        }
        
        # Migrate diagnostic
        if diagnostic_id:
            try:
                diagnostic = self.db.load_json("diagnostics", diagnostic_id)
                if diagnostic and not diagnostic.get("user_id"):
                    diagnostic["user_id"] = user_id
                    self.db.save_json("diagnostics", diagnostic_id, diagnostic)
                    migrated["diagnostic"] = True
            except:
                pass
        
        # Migrate snapshot
        if snapshot_id:
            try:
                snapshot = self.db.load_json("snapshots", snapshot_id)
                if snapshot and not snapshot.get("user_id"):
                    snapshot["user_id"] = user_id
                    self.db.save_json("snapshots", snapshot_id, snapshot)
                    migrated["snapshot"] = True
            except:
                pass
        
        # Migrate blueprint
        if blueprint_id:
            try:
                blueprint = self.db.load_json("blueprints", blueprint_id)
                if blueprint and not blueprint.get("user_id"):
                    blueprint["user_id"] = user_id
                    self.db.save_json("blueprints", blueprint_id, blueprint)
                    migrated["blueprint"] = True
            except:
                pass
        
        return migrated

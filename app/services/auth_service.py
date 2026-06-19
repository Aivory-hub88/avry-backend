"""
Authentication service — PostgreSQL-backed (with JSON fallback).

Users and sessions are stored in PostgreSQL when DATABASE_URL is set.
Diagnostics, snapshots, blueprints, and payments remain in JSON files
(unchanged from the original implementation).
"""

import os
import bcrypt
import jwt
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.models.user import (
    UserCreate, UserLogin, UserResponse,
    Session, TokenPair, AuthResponse,
)
from app.utils.id_generator import generate_id

# Lazy import so the module loads even if asyncpg isn't installed yet
try:
    from app.database import pg_service as pg
    _PG_AVAILABLE = True
except ImportError:
    _PG_AVAILABLE = False

# ── JWT config ────────────────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60      # 1 hour (was 15 min)
REFRESH_TOKEN_EXPIRE_DAYS = 7


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AuthService:
    """Handles authentication, user management, and JWT tokens."""

    def __init__(self, db_service):
        self.db = db_service  # kept for diagnostics / payments / blueprints

    # ── Helpers ───────────────────────────────────────────────────────────────

    def hash_password(self, password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()

    def verify_password(self, password: str, password_hash: str) -> bool:
        return bcrypt.checkpw(password.encode(), password_hash.encode())

    def create_access_token(self, user: dict) -> str:
        payload = {
            "user_id":      user.get("user_id") or user.get("id"),
            "email":        user["email"],
            "account_type": user.get("account_type", "free"),
            "exp": _now() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
            "iat": _now(),
        }
        return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

    def create_refresh_token(self, user_id: str, session_id: str) -> str:
        payload = {
            "user_id":    user_id,
            "session_id": session_id,
            "exp": _now() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
            "iat": _now(),
        }
        return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

    def verify_token(self, token: str) -> Optional[dict]:
        try:
            return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            return None

    # ── Tier helpers (unchanged logic) ───────────────────────────────────────

    def _compute_tier(self, user_id: str, account_type: str) -> dict:
        """Return tier / credits info for a user, checking payments in JSON store."""
        if account_type == "superadmin":
            return dict(
                tier="enterprise", is_subscribed=True,
                has_diagnostic=True, has_snapshot=True, has_blueprint=True,
                credits=2000, credits_max=2000,
            )

        payments    = self.db.load_all_json("payments")
        diagnostics = self.db.load_all_json("diagnostics")
        snapshots   = self.db.load_all_json("snapshots")
        blueprints  = self.db.load_all_json("blueprints")

        up = [p for p in payments if p.get("user_id") == user_id and p.get("status") == "paid"]
        bundle = any(p.get("product") == "ai_bundle"          for p in up)
        has_snap_pay  = any(p.get("product") == "ai_snapshot" for p in up) or bundle
        has_bp_pay    = any(p.get("product") == "ai_blueprint" for p in up) or bundle
        has_sub_pay   = any(p.get("product") == "step3_subscription" for p in up)

        has_diagnostic = any(d.get("user_id") == user_id for d in diagnostics)
        has_snapshot   = any(s.get("user_id") == user_id for s in snapshots)
        has_blueprint  = any(b.get("user_id") == user_id for b in blueprints)

        if has_sub_pay:
            return dict(tier="enterprise", is_subscribed=True,
                        has_diagnostic=has_diagnostic, has_snapshot=has_snapshot,
                        has_blueprint=has_blueprint, credits=2000, credits_max=2000)
        if has_bp_pay:
            return dict(tier="blueprint", is_subscribed=False,
                        has_diagnostic=has_diagnostic, has_snapshot=has_snapshot,
                        has_blueprint=has_blueprint, credits=100, credits_max=100)
        if has_snap_pay:
            return dict(tier="snapshot", is_subscribed=False,
                        has_diagnostic=has_diagnostic, has_snapshot=has_snapshot,
                        has_blueprint=has_blueprint, credits=50, credits_max=50)
        return dict(tier="free", is_subscribed=False,
                    has_diagnostic=has_diagnostic, has_snapshot=has_snapshot,
                    has_blueprint=has_blueprint, credits=10, credits_max=10)

    def _build_user_response(self, user: dict) -> UserResponse:
        user_id      = user.get("user_id") or user.get("id")
        account_type = user.get("account_type", "free")
        tier_info    = self._compute_tier(user_id, account_type)

        created = user.get("created_at")
        if isinstance(created, str):
            created = datetime.fromisoformat(created)

        return UserResponse(
            user_id=user_id,
            email=user["email"],
            account_type=account_type,
            company_name=user.get("company_name"),
            created_at=created or _now(),
            **tier_info,
        )

    # ── PostgreSQL user lookup ────────────────────────────────────────────────

    async def _pg_get_user_by_email(self, email: str) -> Optional[dict]:
        if not _PG_AVAILABLE:
            return None
        try:
            if not await pg.is_available():
                return None
            return await pg.get_user_by_email(email)
        except Exception as e:
            print(f"[PG] get_user_by_email error: {e}")
            return None

    async def _pg_get_user_by_id(self, user_id: str) -> Optional[dict]:
        if not _PG_AVAILABLE:
            return None
        try:
            if not await pg.is_available():
                return None
            return await pg.get_user_by_id(user_id)
        except Exception as e:
            print(f"[PG] get_user_by_id error: {e}")
            return None

    # ── Core auth operations ──────────────────────────────────────────────────

    async def register(self, user_data: UserCreate) -> AuthResponse:
        pg_up = _PG_AVAILABLE and await pg.is_available()

        # ── Duplicate-check ───────────────────────────────────────────────────
        if pg_up:
            if await pg.email_exists(user_data.email):
                raise ValueError("Email already registered")
        else:
            users = self.db.load_all_json("users")
            if any(u.get("email") == user_data.email for u in users):
                raise ValueError("Email already registered")

        user_id       = generate_id("user")
        password_hash = self.hash_password(user_data.password)

        # ── Persist user ──────────────────────────────────────────────────────
        if pg_up:
            row = await pg.insert_user(
                user_id=user_id,
                email=user_data.email,
                password_hash=password_hash,
                account_type="free",
                company_name=user_data.company_name,
            )
            user = {
                "user_id":      row["id"],
                "id":           row["id"],
                "email":        row["email"],
                "account_type": row["account_type"],
                "company_name": row.get("company_name"),
                "password_hash": password_hash,
                "created_at":   row["created_at"],
                "updated_at":   row["updated_at"],
            }
        else:
            user = {
                "user_id":      user_id,
                "email":        user_data.email,
                "password_hash": password_hash,
                "account_type": "free",
                "company_name": user_data.company_name,
                "created_at":   _now().isoformat(),
                "updated_at":   _now().isoformat(),
            }
            self.db.save_json("users", user_id, user)

        # ── Session & tokens ──────────────────────────────────────────────────
        session_id    = generate_id("session")
        refresh_token = self.create_refresh_token(user_id, session_id)
        access_token  = self.create_access_token(user)
        expires_at    = _now() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

        if pg_up:
            await pg.insert_session(session_id, user_id, refresh_token, expires_at)
        else:
            self.db.save_json("sessions", session_id, {
                "session_id":    session_id,
                "user_id":       user_id,
                "refresh_token": refresh_token,
                "expires_at":    expires_at.isoformat(),
                "created_at":    _now().isoformat(),
            })

        return AuthResponse(
            user=self._build_user_response(user),
            tokens=TokenPair(access_token=access_token, refresh_token=refresh_token),
        )

    async def login(self, credentials: UserLogin) -> AuthResponse:
        pg_up = _PG_AVAILABLE and await pg.is_available()

        # ── Find user ─────────────────────────────────────────────────────────
        if pg_up:
            user = await pg.get_user_by_email(credentials.email)
            if user:
                # Map 'id' → 'user_id' for consistency
                user = dict(user)
                user.setdefault("user_id", user.get("id"))
        else:
            users = self.db.load_all_json("users")
            user  = next((u for u in users if u.get("email") == credentials.email), None)

        if not user:
            raise ValueError("Invalid email or password")

        if not self.verify_password(credentials.password, user["password_hash"]):
            raise ValueError("Invalid email or password")

        # ── Session & tokens ──────────────────────────────────────────────────
        user_id       = user.get("user_id") or user.get("id")
        session_id    = generate_id("session")
        refresh_token = self.create_refresh_token(user_id, session_id)
        access_token  = self.create_access_token(user)
        expires_at    = _now() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

        if pg_up:
            await pg.insert_session(session_id, user_id, refresh_token, expires_at)
        else:
            self.db.save_json("sessions", session_id, {
                "session_id":    session_id,
                "user_id":       user_id,
                "refresh_token": refresh_token,
                "expires_at":    expires_at.isoformat(),
                "created_at":    _now().isoformat(),
            })

        return AuthResponse(
            user=self._build_user_response(user),
            tokens=TokenPair(access_token=access_token, refresh_token=refresh_token),
        )

    async def refresh_access_token(self, refresh_token: str) -> TokenPair:
        payload = self.verify_token(refresh_token)
        if not payload:
            raise ValueError("Invalid or expired refresh token")

        session_id = payload.get("session_id")
        user_id    = payload.get("user_id")
        pg_up      = _PG_AVAILABLE and await pg.is_available()

        if pg_up:
            session = await pg.get_session_by_id(session_id)
        else:
            session = self.db.load_json("sessions", session_id)

        if not session:
            raise ValueError("Session not found")

        expires = session.get("expires_at")
        if isinstance(expires, str):
            expires = datetime.fromisoformat(expires)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if _now() > expires:
            raise ValueError("Session expired")

        if pg_up:
            user = await pg.get_user_by_id(user_id)
            if user:
                user = dict(user)
                user.setdefault("user_id", user.get("id"))
        else:
            user = self.db.load_json("users", user_id)

        if not user:
            raise ValueError("User not found")

        return TokenPair(
            access_token=self.create_access_token(user),
            refresh_token=refresh_token,
        )

    async def logout(self, refresh_token: str) -> bool:
        payload = self.verify_token(refresh_token)
        if not payload:
            return False

        session_id = payload.get("session_id")
        pg_up = _PG_AVAILABLE and await pg.is_available()

        if pg_up:
            return await pg.delete_session(session_id)
        else:
            return self.db.delete_json("sessions", session_id)

    async def get_current_user(self, access_token: str) -> Optional[UserResponse]:
        payload = self.verify_token(access_token)
        if not payload:
            return None

        user_id = payload.get("user_id")
        pg_up   = _PG_AVAILABLE and await pg.is_available()

        if pg_up:
            user = await pg.get_user_by_id(user_id)
            if user:
                user = dict(user)
                user.setdefault("user_id", user.get("id"))
        else:
            user = self.db.load_json("users", user_id)

        if not user:
            return None

        return self._build_user_response(user)

    async def get_user_from_refresh_token(self, refresh_token: str) -> Optional[dict]:
        payload = self.verify_token(refresh_token)
        if not payload:
            return None
        pg_up = _PG_AVAILABLE and await pg.is_available()
        if pg_up:
            return await pg.get_user_from_refresh_token(refresh_token)
        session_id = payload.get("session_id")
        session    = self.db.load_json("sessions", session_id)
        if not session:
            return None
        return self.db.load_json("users", session.get("user_id"))

    async def migrate_ids_to_user(
        self, user_id: str,
        diagnostic_id: Optional[str] = None,
        snapshot_id: Optional[str] = None,
        blueprint_id: Optional[str] = None,
    ) -> dict:
        migrated = {"diagnostic": False, "snapshot": False, "blueprint": False}
        for kind, item_id in [
            ("diagnostic", diagnostic_id),
            ("snapshot",   snapshot_id),
            ("blueprint",  blueprint_id),
        ]:
            if not item_id:
                continue
            try:
                rec = self.db.load_json(f"{kind}s", item_id)
                if rec and not rec.get("user_id"):
                    rec["user_id"] = user_id
                    self.db.save_json(f"{kind}s", item_id, rec)
                    migrated[kind] = True
            except Exception:
                pass
        return migrated

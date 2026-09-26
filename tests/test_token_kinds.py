"""Refresh tokens must not work as bearer access tokens (and vice versa).

Both kinds share JWT_SECRET, so before token_kinds a refresh token (30-day,
still valid after logout) passed every signature-only bearer check.
"""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException

from app.routes import deps
from app.services import auth_service as auth_mod
from app.services.auth_service import AuthService, JWT_SECRET, JWT_ALGORITHM
from app.services.token_kinds import is_access_payload, is_refresh_payload


class _FakeDB:
    def __init__(self):
        self.store = {}

    def load_json(self, collection, key):
        return self.store.get((collection, key))

    def save_json(self, collection, key, value):
        self.store[(collection, key)] = value


def _legacy(payload: dict) -> str:
    """A token as issued before tokens carried "type"."""
    body = {**payload, "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
    return jwt.encode(body, JWT_SECRET, algorithm=JWT_ALGORITHM)


class TokenKindsTest(unittest.TestCase):
    def setUp(self):
        auth_mod._PG_AVAILABLE = False
        self.db = _FakeDB()
        self.svc = AuthService(self.db)
        self.user = {"user_id": "u1", "email": "u1@example.com", "account_type": "free"}
        self.db.save_json("users", "u1", dict(self.user))

    def test_new_tokens_carry_their_kind(self):
        access = jwt.decode(self.svc.create_access_token(self.user), JWT_SECRET, algorithms=[JWT_ALGORITHM])
        refresh = jwt.decode(self.svc.create_refresh_token("u1", "s1"), JWT_SECRET, algorithms=[JWT_ALGORITHM])
        self.assertEqual(access["type"], "access")
        self.assertEqual(refresh["type"], "refresh")

    def test_verify_token_accepts_access_rejects_refresh(self):
        self.assertIsNotNone(self.svc.verify_token(self.svc.create_access_token(self.user)))
        self.assertIsNone(self.svc.verify_token(self.svc.create_refresh_token("u1", "s1")))

    def test_legacy_tokens_are_classified_by_session_id(self):
        legacy_access = _legacy({"user_id": "u1", "email": "u1@example.com", "account_type": "free"})
        legacy_refresh = _legacy({"user_id": "u1", "session_id": "s1"})
        self.assertIsNotNone(self.svc.verify_token(legacy_access))
        self.assertIsNone(self.svc.verify_token(legacy_refresh))
        self.assertIsNotNone(self.svc.verify_refresh_token(legacy_refresh))
        self.assertIsNone(self.svc.verify_refresh_token(legacy_access))

    def test_impersonation_tokens_are_neither(self):
        p = {"type": "impersonation", "admin_user_id": "a", "target_user_id": "u1", "access_mode": "read", "session_id": "x"}
        self.assertFalse(is_access_payload(p))
        self.assertFalse(is_refresh_payload(p))

    def test_refresh_flow_rejects_an_access_token(self):
        with self.assertRaises(ValueError):
            asyncio.new_event_loop().run_until_complete(self.svc.refresh_access_token(self.svc.create_access_token(self.user)))

    def test_deps_current_payload_rejects_refresh(self):
        # deps reads its own JWT_SECRET env var; sign with that one.
        refresh = jwt.encode(
            {"user_id": "u1", "session_id": "s1", "type": "refresh", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
            deps.JWT_SECRET,
            algorithm=deps.JWT_ALGORITHM,
        )
        access = jwt.encode(
            {"user_id": "u1", "account_type": "free", "type": "access", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
            deps.JWT_SECRET,
            algorithm=deps.JWT_ALGORITHM,
        )
        with self.assertRaises(HTTPException) as ctx:
            deps.current_payload(f"Bearer {refresh}")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(deps.current_payload(f"Bearer {access}")["user_id"], "u1")



class AccessTtlTest(unittest.TestCase):
    def test_default_is_twelve_hours_and_env_is_clamped(self):
        import os
        from app.services import auth_service as mod

        saved = os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES")
        try:
            os.environ.pop("ACCESS_TOKEN_EXPIRE_MINUTES", None)
            self.assertEqual(mod._access_ttl_minutes(), 720)
            for raw, want in (("60", 60), ("1", 5), ("99999", 1440), ("nope", 720)):
                os.environ["ACCESS_TOKEN_EXPIRE_MINUTES"] = raw
                self.assertEqual(mod._access_ttl_minutes(), want)
        finally:
            if saved is None:
                os.environ.pop("ACCESS_TOKEN_EXPIRE_MINUTES", None)
            else:
                os.environ["ACCESS_TOKEN_EXPIRE_MINUTES"] = saved

    def test_issued_access_token_lives_twelve_hours(self):
        svc = AuthService(_FakeDB())
        p = jwt.decode(svc.create_access_token({"user_id": "u1", "email": "a@b.co"}), JWT_SECRET, algorithms=[JWT_ALGORITHM])
        self.assertEqual(p["exp"] - p["iat"], 12 * 3600)


if __name__ == "__main__":
    unittest.main()

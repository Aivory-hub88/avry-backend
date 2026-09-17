"""Sliding refresh-window tests (30-day session for active users).

Uses the JSON fallback store (dict-backed, no Postgres, no files) so the
sliding-expiry logic is exercised without any infrastructure.
"""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from app.services import auth_service as auth_mod
from app.services.auth_service import AuthService, REFRESH_TOKEN_EXPIRE_DAYS


class _FakeDB:
    def __init__(self):
        self.store = {}

    def load_json(self, collection, key):
        return self.store.get((collection, key))

    def save_json(self, collection, key, value):
        self.store[(collection, key)] = value


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class SlidingRefreshTest(unittest.TestCase):
    def setUp(self):
        # Force the JSON-fallback path regardless of environment.
        auth_mod._PG_AVAILABLE = False
        self.db = _FakeDB()
        self.svc = AuthService(self.db)
        self.user_id = "user_test123"
        self.session_id = "session_test123"
        self.db.save_json("users", self.user_id, {
            "user_id": self.user_id, "email": "t@example.com",
        })

    def _login_session(self, days_valid=1):
        rt = self.svc.create_refresh_token(self.user_id, self.session_id)
        self.db.save_json("sessions", self.session_id, {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "refresh_token": rt,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=days_valid)).isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return rt

    def test_window_is_30_days(self):
        self.assertEqual(REFRESH_TOKEN_EXPIRE_DAYS, 30)

    def test_successful_refresh_slides_expiry(self):
        rt = self._login_session(days_valid=1)
        pair = _run(self.svc.refresh_access_token(rt))
        self.assertTrue(pair.access_token)
        # Same (unrotated) refresh token back.
        self.assertEqual(pair.refresh_token, rt)
        row = self.db.load_json("sessions", self.session_id)
        new_exp = datetime.fromisoformat(row["expires_at"])
        remaining = (new_exp - datetime.now(timezone.utc)).days
        self.assertGreaterEqual(remaining, 29)

    def test_expired_session_still_rejected(self):
        rt = self.svc.create_refresh_token(self.user_id, self.session_id)
        self.db.save_json("sessions", self.session_id, {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "refresh_token": rt,
            # JWT itself still valid (30d) but the server-side row lapsed.
            "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            "created_at": (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(),
        })
        with self.assertRaises(ValueError):
            _run(self.svc.refresh_access_token(rt))


if __name__ == "__main__":
    unittest.main()

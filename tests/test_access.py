#!/usr/bin/env python3
"""
app/services/access.py — the one rule for "what may this account use", and the
strict GET /api/v1/entitlements/access that server-side gates call.

Run from the avry-backend root:
    python3 -m unittest tests.test_access
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import access  # noqa: E402


class Decide(unittest.TestCase):
    def test_subscription_wins_and_aliases_are_canonical(self):
        for row, want in [("operational", "operational"), ("business", "business"),
                          ("enterprise", "enterprise"), ("pro", "business"), ("foundation", "operational")]:
            s = access.decide("user", row_tier=row)
            self.assertEqual((s["tier"], s["subscription_tier"], s["paid"]), (want, want, True), row)
            self.assertTrue(s["is_subscribed"])

    def test_one_time_buyers_are_paid_and_never_expire(self):
        s = access.decide("user", row_tier="free", features=["snapshot", "blueprint"], expired=True)
        self.assertEqual((s["tier"], s["subscription_tier"], s["paid"]), ("blueprint", None, True))
        self.assertEqual(access.decide("user", features=["snapshot"])["tier"], "snapshot")

    def test_no_plan_is_free_and_unpaid(self):
        for args in [dict(), dict(row_tier="free"), dict(row_tier="business", expired=True), dict(row_tier="junk")]:
            s = access.decide("user", **args)
            self.assertEqual((s["tier"], s["paid"], s["is_subscribed"]), ("free", False, False), args)

    def test_partner_free_period_features_do_not_count_after_it_lapses(self):
        s = access.decide("partner", row_tier="enterprise", features=["snapshot", "blueprint"], expired=True)
        self.assertEqual((s["tier"], s["paid"]), ("free", False))
        self.assertEqual(access.decide("partner", row_tier="enterprise")["tier"], "enterprise")

    def test_staff_and_demo(self):
        su = access.decide("user", is_superadmin=True)
        self.assertEqual((su["tier"], su["staff"], su["paid"]), ("enterprise", True, True))
        self.assertEqual(access.decide("superadmin")["tier"], "enterprise")
        for kind in ("admin", "employee"):
            s = access.decide(kind)
            self.assertEqual((s["tier"], s["staff"], s["paid"]), ("free", True, True), kind)
        demo = access.decide("demo")
        self.assertEqual((demo["staff"], demo["paid"]), (False, True))


def _conn_returning(row):
    cur = MagicMock()
    cur.fetchone.return_value = row
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn


class Read(unittest.TestCase):
    def test_reads_one_row(self):
        exp = datetime.now(timezone.utc) + timedelta(days=20)
        with patch.object(access, "_connect", return_value=_conn_returning(("user", False, "business", [], exp, False))):
            s = access.read("u1")
        self.assertEqual((s["tier"], s["paid"]), ("business", True))
        self.assertEqual(s["expires_at"], exp.isoformat())

    def test_missing_account_is_none(self):
        with patch.object(access, "_connect", return_value=_conn_returning(None)):
            self.assertIsNone(access.read("ghost"))

    def test_database_trouble_raises_instead_of_answering_free(self):
        with patch.object(access, "_connect", side_effect=RuntimeError("connection refused")):
            with self.assertRaises(access.AccessUnavailable):
                access.read("u1")
        broken = MagicMock()
        broken.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError("relation missing")
        with patch.object(access, "_connect", return_value=broken):
            with self.assertRaises(access.AccessUnavailable):
                access.read("u1")


class Endpoint(unittest.TestCase):
    def setUp(self):
        from app.routes import entitlements
        self.route = entitlements

    def test_ok(self):
        with patch.object(access, "read", return_value=access.decide("user", row_tier="operational")):
            body = self.route.get_access({"user_id": "u1"})
        self.assertTrue(body["ok"] and body["paid"])

    def test_unavailable_is_503(self):
        from fastapi import HTTPException
        with patch.object(access, "read", side_effect=access.AccessUnavailable("down")):
            with self.assertRaises(HTTPException) as ctx:
                self.route.get_access({"user_id": "u1"})
        self.assertEqual(ctx.exception.status_code, 503)

    def test_missing_account_is_401(self):
        from fastapi import HTTPException
        with patch.object(access, "read", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                self.route.get_access({"user_id": "ghost"})
        self.assertEqual(ctx.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()

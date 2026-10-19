#!/usr/bin/env python3
"""
/auth/me (AuthService._build_user_response) must report what a customer has
actually paid for. Payments are written to Postgres (user_tiers, user_credits);
the legacy computation reads only the JSON store, so without the overlay every
paying customer came back as `free`.

Postgres is replaced by patched entitlement lookups. Run via
`python3 -m unittest tests.test_auth_me_entitlements` from the avry-backend root
with the project's requirements installed.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import entitlement_state  # noqa: E402
from app.services.auth_service import AuthService  # noqa: E402


def _service():
    svc = AuthService.__new__(AuthService)
    svc.db = type("Db", (), {"load_all_json": staticmethod(lambda name: [])})()
    return svc


def _me(account_type, live, credits=None):
    user = {
        "user_id": "user_x",
        "email": "x@example.com",
        "account_type": account_type,
        "created_at": "2026-10-01T00:00:00+00:00",
    }
    with patch.object(entitlement_state, "get_entitlements", return_value=live), patch.object(
        entitlement_state, "get_credit_snapshot", return_value=credits
    ):
        return _service()._build_user_response(user)


def _live(tier=None, features=()):
    return {"tier": tier, "features": list(features), "expired": False, "found": True}


class PayingCustomers(unittest.TestCase):
    def test_subscriber_reports_their_plan(self):
        me = _me("paid", _live("business"), {"unlimited": False, "balance": 150, "allowance": 220})
        self.assertEqual(me.tier, "business")
        self.assertTrue(me.is_subscribed)
        self.assertEqual((me.credits, me.credits_max), (150, 220))

    def test_one_time_purchase_turns_on_the_feature(self):
        me = _me("paid", _live(None, ["snapshot", "blueprint"]))
        self.assertEqual(me.tier, "blueprint")
        self.assertTrue(me.has_snapshot and me.has_diagnostic and me.has_blueprint)
        self.assertFalse(me.is_subscribed)

    def test_never_paid_stays_free(self):
        me = _me("free", {"tier": None, "features": [], "expired": False, "found": False})
        self.assertEqual(me.tier, "free")
        self.assertFalse(me.is_subscribed)

    def test_lapsed_subscription_is_not_current(self):
        me = _me("paid", {"tier": None, "features": [], "expired": True, "found": True})
        self.assertEqual(me.tier, "free")

    def test_unreadable_entitlements_keep_the_legacy_answer(self):
        me = _me("paid", None)
        self.assertEqual(me.tier, "free")


class SkippedAccounts(unittest.TestCase):
    def test_superadmin_is_not_overlaid(self):
        with patch.object(entitlement_state, "overlay", side_effect=AssertionError("called")):
            me = _service()._build_user_response(
                {"user_id": "a", "email": "a@x.id", "account_type": "superadmin"}
            )
        self.assertEqual(me.tier, "enterprise")

    def test_lapsed_partner_does_not_get_blueprint_from_free_features(self):
        # A partner's free period grants snapshot+blueprint features; once it
        # lapses they must not count as a paid `blueprint` tier.
        me = _me("partner", _live(None, ["snapshot", "blueprint"]))
        self.assertEqual(me.tier, "free")

    def test_partner_in_free_period_is_enterprise(self):
        me = _me("partner", _live("enterprise", ["snapshot", "blueprint"]))
        self.assertEqual(me.tier, "enterprise")


if __name__ == "__main__":
    unittest.main()

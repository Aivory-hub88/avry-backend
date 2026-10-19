#!/usr/bin/env python3
"""
Runs-per-purchase against a real Postgres. Skipped unless
FEATURE_USES_TEST_DATABASE_URL points at a throwaway database (identity and
billing tables there are dropped and recreated).

    FEATURE_USES_TEST_DATABASE_URL=postgresql://… python -m unittest discover -s tests -p test_feature_uses_db.py
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB = os.getenv("FEATURE_USES_TEST_DATABASE_URL")


@unittest.skipUnless(DB, "FEATURE_USES_TEST_DATABASE_URL not set")
class FeatureUses(unittest.TestCase):
    def setUp(self):
        os.environ["DATABASE_URL"] = DB
        import psycopg2

        from app.services import feature_uses

        self.fu = feature_uses
        feature_uses._schema_ready = False
        conn = psycopg2.connect(DB)
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS identity CASCADE; DROP SCHEMA IF EXISTS billing CASCADE")
            cur.execute("CREATE SCHEMA identity")
            cur.execute(
                "CREATE TABLE identity.users (id TEXT PRIMARY KEY, email TEXT, account_type TEXT, "
                "is_superadmin BOOLEAN DEFAULT false, updated_at TIMESTAMPTZ)"
            )
            cur.execute(
                "CREATE TABLE identity.user_tiers (user_id TEXT PRIMARY KEY, tier TEXT, "
                "features JSONB DEFAULT '[]'::jsonb, expires_at TIMESTAMPTZ, updated_at TIMESTAMPTZ)"
            )
            cur.execute(
                "INSERT INTO identity.users (id, account_type) VALUES "
                "('buyer','free'), ('legacy','paid'), ('partner','partner'), ('lapsed_partner','partner'), ('boss','superadmin')"
            )
            cur.execute(
                """
                INSERT INTO identity.user_tiers (user_id, tier, features, expires_at) VALUES
                ('legacy', NULL, '["snapshot"]', NULL),
                ('partner', 'enterprise', '["snapshot","blueprint"]', now() + interval '30 days'),
                ('lapsed_partner', 'enterprise', '["snapshot","blueprint"]', now() - interval '1 day')
                """
            )
        conn.commit()
        conn.close()

    def _grant(self, user, product, order):
        from app.routes import entitlements

        entitlements._schema_ready = False
        return entitlements.grant_entitlement(
            entitlements.GrantRequest(user_id=user, product=product, order_id=order)
        )

    def test_assessment_purchase_gives_three_runs_then_stops(self):
        self._grant("buyer", "ai_snapshot", "o1")
        self.assertEqual(self.fu.status("buyer")["snapshot"]["remaining"], 3)
        self.assertEqual(self.fu.status("buyer")["blueprint"]["remaining"], 0)
        for job in ("j1", "j2", "j3"):
            self.fu.consume("buyer", "snapshot", job)
        with self.assertRaises(self.fu.NoUsesLeft):
            self.fu.consume("buyer", "snapshot", "j4")
        # Re-polling a finished job never charges again.
        again = self.fu.consume("buyer", "snapshot", "j2")
        self.assertFalse(again["charged"])
        self.assertEqual(self.fu.status("buyer")["snapshot"]["used"], 3)

    def test_complete_package_is_three_full_cycles_and_grant_is_idempotent(self):
        self._grant("buyer", "ai_fullstack", "o2")
        self._grant("buyer", "ai_fullstack", "o2")  # Midtrans retry
        st = self.fu.status("buyer")
        self.assertEqual((st["snapshot"]["remaining"], st["blueprint"]["remaining"]), (3, 3))

    def test_buying_again_adds_three_more(self):
        self._grant("buyer", "ai_blueprint", "o3")
        self.fu.consume("buyer", "blueprint", "b1")
        self._grant("buyer", "ai_blueprint", "o4")
        self.assertEqual(self.fu.status("buyer")["blueprint"]["remaining"], 5)

    def test_someone_elses_job_id_is_never_delivered(self):
        self._grant("buyer", "ai_snapshot", "o5")
        self._grant("legacy", "ai_snapshot", "o6")
        self.fu.consume("buyer", "snapshot", "shared-job")
        with self.assertRaises(self.fu.NoUsesLeft):
            self.fu.consume("legacy", "snapshot", "shared-job")

    def test_pre_ledger_buyers_get_three_runs(self):
        st = self.fu.status("legacy")
        self.assertEqual(st["snapshot"]["remaining"], 3)
        self.assertEqual(st["blueprint"]["remaining"], 0)

    def test_active_partner_and_superadmin_are_unlimited_lapsed_partner_is_not(self):
        for user in ("partner", "boss"):
            r = self.fu.consume(user, "snapshot", f"{user}-job")
            self.assertTrue(r["unlimited"])
        self.assertFalse(self.fu.status("lapsed_partner")["snapshot"]["unlimited"])
        # Its free-period features were not a purchase: no runs once it lapses.
        self.assertEqual(self.fu.status("lapsed_partner")["snapshot"]["remaining"], 0)


if __name__ == "__main__":
    unittest.main()

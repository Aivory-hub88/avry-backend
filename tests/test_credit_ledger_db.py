#!/usr/bin/env python3
"""
Credit ledger against a real Postgres. Skipped unless
CREDIT_LEDGER_TEST_DATABASE_URL points at a throwaway database (identity and
billing schemas there are dropped and recreated).

    CREDIT_LEDGER_TEST_DATABASE_URL=postgresql://… python -m unittest discover -s tests -p test_credit_ledger_db.py
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB = os.getenv("CREDIT_LEDGER_TEST_DATABASE_URL")


@unittest.skipUnless(DB, "CREDIT_LEDGER_TEST_DATABASE_URL not set")
class CreditLedger(unittest.TestCase):
    def setUp(self):
        os.environ["DATABASE_URL"] = DB
        import psycopg2

        from app.services import credit_service, tiers

        self.cs = credit_service
        self.tiers = tiers
        credit_service._schema_ready = False
        conn = psycopg2.connect(DB)
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS identity CASCADE; DROP SCHEMA IF EXISTS billing CASCADE")
            cur.execute("CREATE SCHEMA identity; CREATE SCHEMA billing")
            cur.execute(
                "CREATE TABLE identity.users (id TEXT PRIMARY KEY, account_type TEXT, "
                "is_superadmin BOOLEAN DEFAULT false)"
            )
            cur.execute(
                "CREATE TABLE identity.user_tiers (user_id TEXT PRIMARY KEY, tier TEXT, expires_at TIMESTAMPTZ)"
            )
            cur.execute(
                "INSERT INTO identity.users (id, account_type) VALUES "
                "('biz','paid'), ('ops','paid'), ('old','paid'), ('boss','superadmin')"
            )
            cur.execute(
                "INSERT INTO identity.user_tiers (user_id, tier) VALUES "
                "('biz','business'), ('ops','operational'), ('old','business')"
            )
        conn.commit()
        self.conn = conn

    def tearDown(self):
        self.conn.close()

    def _sql(self, q, args=()):
        with self.conn.cursor() as cur:
            cur.execute(q, args)
        self.conn.commit()

    def test_new_account_gets_tier_allowance_and_no_extra(self):
        st = self.cs.get_status("biz")
        self.assertEqual((st["monthly_balance"], st["extra_balance"], st["balance"]), (6500, 0, 6500))
        self.assertEqual(self.cs.get_status("ops")["allowance"], 1500)

    def test_spend_draws_monthly_first_then_extra(self):
        self.cs.grant("ops", 1000, reason="purchase")
        r = self.cs.consume("ops", 1400, "agent_message", {"tokens": 18000000})
        self.assertEqual((r["monthly_balance"], r["extra_balance"]), (100, 1000))
        r = self.cs.consume("ops", 300, "agent_message")
        self.assertEqual((r["monthly_balance"], r["extra_balance"]), (0, 800))
        st = self.cs.get_status("ops")
        self.assertEqual(st["credits_used_this_period"], 1700)
        self.assertEqual(st["extra_used_this_period"], 200)
        self.assertEqual(st["tokens_used_this_period"], 18000000)

    def test_short_balance_refuses_unless_partial(self):
        with self.assertRaises(self.cs.InsufficientCredits):
            self.cs.consume("ops", 1501, "agent_message")
        r = self.cs.consume("ops", 1501, "agent_message", allow_partial=True)
        self.assertEqual((r["charged"], r["balance"]), (1500, 0))
        r = self.cs.consume("ops", 5, "agent_message", allow_partial=True)
        self.assertEqual(r["charged"], 0)

    def test_new_month_refills_monthly_and_keeps_bought_credits(self):
        self.cs.grant("biz", 1700, reason="purchase")
        self.cs.consume("biz", 7000, "agent_message")  # all monthly + 500 extra
        self._sql("UPDATE billing.user_credits SET period_start = period_start - interval '1 month'")
        st = self.cs.get_status("biz")
        self.assertEqual((st["monthly_balance"], st["extra_balance"]), (6500, 1200))

    def test_legacy_row_is_converted_once(self):
        self.cs.get_status("biz")  # creates billing tables
        # Old units: business allowance 220, all of it left, plus 50 bought.
        self._sql(
            "INSERT INTO billing.user_credits (user_id, balance, allowance, period_start, units_version) "
            "VALUES ('old', 270, 220, date_trunc('month', now())::date, 1), "
            "       ('ops', 40, 80, date_trunc('month', now())::date, 1)"
        )
        st = self.cs.get_status("old")
        self.assertEqual((st["monthly_balance"], st["extra_balance"]), (6500, 350))
        st = self.cs.get_status("old")  # idempotent
        self.assertEqual((st["monthly_balance"], st["extra_balance"]), (6500, 350))
        # Half the old operational allowance left -> half the new one.
        st = self.cs.get_status("ops")
        self.assertEqual((st["monthly_balance"], st["extra_balance"]), (750, 0))

    def test_superadmin_is_unlimited(self):
        self.assertTrue(self.cs.consume("boss", 999, "agent_message")["unlimited"])


if __name__ == "__main__":
    unittest.main()
